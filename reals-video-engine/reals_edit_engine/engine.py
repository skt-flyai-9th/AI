"""Video Edit Engine 파사드 — 두 실행 모드 + idempotency (구현 문서 2.5·26).

엔진 안에는 LLM이 없다. 입력은 검증된 구조화 요청, 출력은 파일 + Manifest.
"""
from __future__ import annotations
import hashlib, json, os, pathlib, uuid


def _log(msg: str):
    if os.environ.get("REALS_QUIET") != "1":
        print(f"  [engine] {msg}", flush=True)

from .contracts import (CutAssemblyRequest, EngineResult, ExecutionMode,
                        FinalAudioPolicy, FinalRenderRequest, MediaFileRef,
                        OverlayType, QcStatus, RenderManifest)
from .cut_assembly import CutAssemblyError, SemanticSegmenter, run_cut_assembly
from .ffmpeg_graph import build_render_plan, map_produced_to_output_ms
from .media import MediaError, media_ref, run
from .qc import post_render_qc
from .registries import ENGINE_VERSION, Registries, RegistryError
from .sfx import SfxResolveError, SfxResolver
from .subtitle_layout import (LayoutError, StaticAvoidMapProvider, AvoidMapProvider,
                              build_ass, layout_overlay)
from .validator import ValidationError, expected_duration_ms, validate_recipe


def _available_ram_gb() -> float:
    from .model_adapters.device import available_ram_gb
    return available_ram_gb()


def _idem_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def _artifact_key(idempotency_key: str) -> str:
    """Return a stable filename-safe key for FFmpeg and temporary render artifacts."""
    return hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]


class VideoEditEngine:
    def __init__(self, root: str | pathlib.Path,
                 segmenter: SemanticSegmenter | None = None,
                 sfx_resolver: SfxResolver | None = None,
                 avoid_provider: AvoidMapProvider | None = None):
        self.root = pathlib.Path(root)
        self.reg = Registries(root)
        self.segmenter = segmenter
        self.sfx_resolver = sfx_resolver
        self.avoid_provider = avoid_provider or StaticAvoidMapProvider()
        self.workdir = self.root / ".work"
        self.workdir.mkdir(exist_ok=True)
        self._jobs_path = self.workdir / "jobs.json"
        self._jobs = json.loads(self._jobs_path.read_text(encoding="utf-8")) if self._jobs_path.exists() else {}

    def _save_job(self, key: str, record: dict):
        self._jobs[key] = record
        self._jobs_path.write_text(json.dumps(self._jobs, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── 모드 1: CUT_ASSEMBLY ─────────────────────────────────────────
    def cut_assembly(self, req: CutAssemblyRequest) -> EngineResult:
        key = req.idempotency_key or _idem_key(
            req.shoot_session_id, *(c.file.sha256 for c in req.raw_cuts),
            str(req.guide_template_version), ENGINE_VERSION)
        cached = self._jobs.get(key)
        if cached and cached.get("status") == "COMPLETED" and \
           pathlib.Path(cached["assembled_path"]).exists():
            from .contracts import CutManifest
            return EngineResult(job_id=req.job_id, execution_mode=ExecutionMode.CUT_ASSEMBLY,
                                status="COMPLETED", deliverable=True,
                                cut_manifest=CutManifest(**cached["cut_manifest"]))
        if self.segmenter is None:
            return EngineResult(job_id=req.job_id, execution_mode=ExecutionMode.CUT_ASSEMBLY,
                                status="FAILED", error="segmenter 어댑터 미설정")
        try:
            manifest = run_cut_assembly(req, self.reg, self.segmenter, str(self.workdir))
        except (CutAssemblyError, MediaError, RegistryError) as e:
            return EngineResult(job_id=req.job_id, execution_mode=ExecutionMode.CUT_ASSEMBLY,
                                status="FAILED", error=str(e))
        ok = manifest.qc_status == QcStatus.PASS
        self._save_job(key, {"status": "COMPLETED" if ok else "QC_FAILED",
                             "assembled_path": manifest.assembled_file.path,
                             "cut_manifest": json.loads(manifest.model_dump_json())})
        return EngineResult(job_id=req.job_id, execution_mode=ExecutionMode.CUT_ASSEMBLY,
                            status="COMPLETED" if ok else "BLOCKED", deliverable=ok,
                            cut_manifest=manifest,
                            error="" if ok else "intermediate QC 실패")

    # ── 모드 2: FINAL_RENDER ─────────────────────────────────────────
    def final_render(self, req: FinalRenderRequest, out_path: str) -> EngineResult:
        recipe = req.edit_recipe
        recipe_hash = "sha256:" + hashlib.sha256(
            recipe.model_dump_json().encode()).hexdigest()
        key = req.idempotency_key or _idem_key(
            req.produced_video.sha256, recipe_hash, ENGINE_VERSION)
        artifact_key = _artifact_key(key)

        # 1. preflight + Recipe Validator — 실패 시 렌더 진입 금지
        try:
            src = media_ref(req.produced_video.file_id, req.produced_video.path)
            if req.produced_video.sha256 and src.sha256 != req.produced_video.sha256:
                raise MediaError("입력 해시 불일치 — 다른 파일이 도착함")
            validate_recipe(recipe, src, self.reg)
            rp = self.reg.render_profile(recipe.render_profile_id)
            amix = self.reg.audio_policy(recipe.audio_mix_policy_id)
            safe = self.reg.safe_area_profile(recipe.safe_area_profile_id)
        except (ValidationError, MediaError, RegistryError) as e:
            return EngineResult(job_id=req.job_id, execution_mode=ExecutionMode.FINAL_RENDER,
                                status="BLOCKED", error=str(e))

        expected_ms = expected_duration_ms(recipe)

        # 조건부 효과 capability 게이트 (구현 문서 17): SMOOTH_ZOOM은 RAM 10GB
        # 미만이면 승인된 fallback(PUNCH_ZOOM)으로 강등하고 manifest에 기록
        capability_fallbacks = []
        if any(e.effect_id == "SMOOTH_ZOOM" for sgm in recipe.segments
               for e in sgm.effects) and _available_ram_gb() < 10.0:
            new_segments = []
            for sgm in recipe.segments:
                effs = [e.model_copy(update={"effect_id": "PUNCH_ZOOM"})
                        if e.effect_id == "SMOOTH_ZOOM" else e for e in sgm.effects]
                new_segments.append(sgm.model_copy(update={"effects": effs}))
            recipe = recipe.model_copy(update={"segments": new_segments})
            capability_fallbacks.append(
                f"SMOOTH_ZOOM→PUNCH_ZOOM (RAM {_available_ram_gb():.1f}GB < 10GB)")

        # 2. Avoid Map (SAM/Face/OCR 어댑터 — 실패 시 기본 Safe Area 배치, 27)
        text_overlays = [o for o in recipe.overlays if o.overlay_type != OverlayType.SFX]
        windows = [(o.start_ms, o.end_ms) for o in text_overlays]
        _log(f"avoid map 분석 (자막 {len(text_overlays)}개 구간)")
        try:
            avoid = self.avoid_provider.analyze(src.path, windows)
            _log(f"avoid map 완료 — {len(avoid.regions)} regions")
        except Exception:
            from .contracts import AvoidMap
            avoid = AvoidMap()

        # 3. Subtitle Layout → ASS
        ass_path = None
        try:
            placed = []
            for o in text_overlays:
                t0 = map_produced_to_output_ms(recipe, o.produced_segment_id,
                                               max(o.start_ms, 0))
                t1 = map_produced_to_output_ms(recipe, o.produced_segment_id, o.end_ms)
                placed.append(layout_overlay(o, t0, t1, self.reg, safe, avoid))
            if placed:
                ass_path = self.workdir / f"subs_{artifact_key}.ass"
                ass_path.write_text(build_ass(placed, self.reg,
                                              (rp["width"], rp["height"])),
                                    encoding="utf-8")
        except (LayoutError, RegistryError) as e:
            return EngineResult(job_id=req.job_id, execution_mode=ExecutionMode.FINAL_RENDER,
                                status="BLOCKED", error=f"자막 배치 실패: {e}")

        # 4. SFX resolve — Provider 실패 시 SILENT fallback (승인된 정책)
        sfx_overlays = [o for o in recipe.overlays if o.overlay_type == OverlayType.SFX]
        sfx_items, sfx_windows, sfx_assets = [], [], []
        final_audio = recipe.final_audio_policy
        fallback_note = ""
        if final_audio == FinalAudioPolicy.SFX_ONLY and sfx_overlays:
            try:
                if self.sfx_resolver is None:
                    raise SfxResolveError("sfx resolver 미설정")
                for o in sfx_overlays:
                    asset = self.sfx_resolver.resolve(o.sfx_intent_id,
                                                      o.sfx_strength.value,
                                                      str(self.workdir))
                    delay = map_produced_to_output_ms(recipe, o.produced_segment_id,
                                                      o.start_ms)
                    sfx_items.append((delay, asset, o.audio_volume_db))
                    sfx_windows.append((delay, delay + asset.duration_ms))
                    sfx_assets.append({"intent": asset.intent_id,
                                       "provider": asset.provider,
                                       "provider_asset_id": asset.provider_asset_id,
                                       "license_ref": asset.license_ref,
                                       "at_ms": delay})
            except SfxResolveError as e:
                final_audio = FinalAudioPolicy.SILENT
                sfx_items, sfx_windows, sfx_assets = [], [], []
                fallback_note = f"SFX provider 실패 → SILENT fallback ({e})"

        # 5. FFmpeg 렌더 — 구간별 렌더 → concat 디먹서 → 마감 (메모리 안전)
        recipe_for_graph = recipe.model_copy(update={"final_audio_policy": final_audio})
        cmds, temps = build_render_plan(
            recipe_for_graph, src.path, str(ass_path) if ass_path else None,
            sfx_items, out_path, rp, amix, expected_ms,
            fonts_dir=str(self.root / "assets" / "fonts"),
            workdir=str(self.workdir), key=artifact_key)
        _log(f"FFmpeg 렌더 시작 — {len(cmds)}단계 "
             f"(구간 {len(recipe.segments)}개 → concat → 마감)")
        try:
            for ci, cmd in enumerate(cmds, 1):
                _log(f"  렌더 {ci}/{len(cmds)}")
                try:
                    run(cmd, timeout=1200)
                except MediaError:
                    run(cmd, timeout=1200)   # deterministic failure 재시도 1회
        except MediaError as e:
            return EngineResult(job_id=req.job_id, execution_mode=ExecutionMode.FINAL_RENDER,
                                status="FAILED", error=str(e))
        finally:
            for t in temps:                  # 중간 파일 정리
                try:
                    pathlib.Path(t).unlink(missing_ok=True)
                except Exception:
                    pass

        _log("렌더 완료 → Post-render QC")
        # 6. Post-render QC — 실패 시 deliverable=False
        qc = post_render_qc(out_path, expected_ms, rp, final_audio, sfx_windows)
        manifest = RenderManifest(
            render_manifest_id=f"renman_{uuid.uuid4().hex[:8]}",
            job_id=req.job_id, recipe_id=recipe.recipe_id,
            recipe_version=recipe.recipe_version, recipe_hash=recipe_hash,
            input_video_sha256=src.sha256,
            output_file=media_ref(f"final_{recipe.recipe_id}", out_path),
            expected_duration_ms=expected_ms,
            concat_order=[s.recipe_segment_id for s in recipe.segments],
            sfx_windows_ms=sfx_windows, sfx_assets=sfx_assets,
            versions={**self.reg.versions(),
                      "render_profile_id": recipe.render_profile_id,
                      "safe_area_profile_id": recipe.safe_area_profile_id,
                      "audio_mix_policy_id": recipe.audio_mix_policy_id,
                      "final_audio_policy_effective": final_audio.value,
                      **({"fallback": fallback_note} if fallback_note else {}),
                      **({"capability_fallbacks": capability_fallbacks}
                         if capability_fallbacks else {})},
            ffmpeg_cmd_sha256="sha256:" + hashlib.sha256(
                json.dumps(cmds).encode()).hexdigest())
        ok = qc.status != QcStatus.FAIL
        self._save_job(key, {"status": "COMPLETED" if ok else "QC_FAILED",
                             "output": out_path})
        return EngineResult(job_id=req.job_id, execution_mode=ExecutionMode.FINAL_RENDER,
                            status="COMPLETED" if ok else "BLOCKED", deliverable=ok,
                            render_manifest=manifest, qc=qc,
                            error="" if ok else "Post-render QC 실패 — 전달 차단")
