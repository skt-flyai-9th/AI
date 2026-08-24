"""End-to-end 데모 — 업로드된 실제 MP4(APT. 챌린지 18.2s)로 두 모드 검증.

시나리오
  A. ONE_TAKE  : normalize → 임의 EditRecipe(자막4·PUNCH_ZOOM·SFX3) → FINAL_RENDER → QC
  B. MULTI_CUT : 원본을 컷 3개로 분할 → CUT_ASSEMBLY(중간 컷 저신뢰도 → 원본 유지)
                 → 결합본에 SILENT 레시피 → FINAL_RENDER → QC
  C. 차단 검증 : 폰트 미지원 글리프(이모지) / 미허용 zoom 범위 → BLOCKED
  D. SFX Provider 장애 → SILENT fallback 렌더
"""
import json, pathlib, sys, uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reals_edit_engine import VideoEditEngine
from reals_edit_engine.contracts import (AvoidMap, AvoidRegion, CutAssemblyRequest,
                                          EditRecipe, EffectApplication,
                                          FinalAudioPolicy, FinalRenderRequest,
                                          FontWeight, GuideSegmentRef, MotionId,
                                          Overlay, OverlayType, RawCut, RecipeSegment,
                                          SourceMode, SfxStrength)
from reals_edit_engine.cut_assembly import MockSegmenter
from reals_edit_engine.media import FFMPEG, media_ref, normalize, probe, run
from reals_edit_engine.sfx import FailingSfxResolver, MockSynthSfxResolver
from reals_edit_engine.subtitle_layout import StaticAvoidMapProvider

SRC = "/root/.claude/uploads/a3ed80de-7cc6-5480-8c81-f078f68e9b03/3c1f5a94-1787520774710_20260823213132.4569313.mp4"
OUT = ROOT / "output"
WORK = ROOT / ".work"


def line(title):
    print("\n" + "=" * 64 + f"\n{title}\n" + "=" * 64)


def show(res):
    print(f"  status={res.status} deliverable={res.deliverable}")
    if res.error:
        print(f"  error: {res.error[:400]}")
    if res.qc:
        for c in res.qc.checks:
            print(f"    [{c.status.value:4}] {c.check_id}: {c.detail}")


# 실제 영상에서 관측한 회피 영역 (실구현에서는 SAM/Face/OCR 어댑터가 생성)
AVOID = AvoidMap(regions=[
    AvoidRegion(x=250, y=350, w=460, h=470, priority=100, label="FACE",
                start_ms=0, end_ms=18240),
    AvoidRegion(x=150, y=150, w=810, h=360, priority=90, label="EXISTING_TEXT_APT",
                start_ms=4000, end_ms=14500),
])

engine = VideoEditEngine(ROOT, segmenter=None,
                         sfx_resolver=MockSynthSfxResolver(),
                         avoid_provider=StaticAvoidMapProvider(AVOID))

# ─────────────────────────────────────────────────────────────────────
line("A. ONE_TAKE — normalize")
inter_profile = engine.reg.render_profile("INTERMEDIATE_VERTICAL_V1")
produced_path = WORK / "produced_one_take.mp4"
info = normalize(SRC, produced_path, inter_profile, keep_audio=True)
print(f"  normalized: {info['width']}x{info['height']} @{info['fps']} "
      f"{info['duration_ms']}ms")
produced = media_ref("prod_one_take_001", produced_path)
D = produced.duration_ms

# ── 임의 EditRecipe (READ-2가 출력했다고 가정한 구조화 결정) ──────────
recipe_a = EditRecipe(
    recipe_id="recipe_demo_apt_001",
    produced_video_id="prod_one_take_001",
    final_audio_policy=FinalAudioPolicy.SFX_ONLY,
    audio_mix_policy_id="SFX_ONLY_V1",
    segments=[
        RecipeSegment(recipe_segment_id="rs_001", produced_segment_id="ps_intro",
                      sequence_index=1, trim_in_ms=300, trim_out_ms=4200,
                      actual_video_evidence="0.3~4.2s 도입 포즈, 시작부 대기 300ms 제거"),
        RecipeSegment(recipe_segment_id="rs_002", produced_segment_id="ps_dance",
                      sequence_index=2, trim_in_ms=4200, trim_out_ms=12400,
                      actual_video_evidence="4.2~12.4s APT 손동작 구간"),
        RecipeSegment(recipe_segment_id="rs_003", produced_segment_id="ps_finale",
                      sequence_index=3, trim_in_ms=12400, trim_out_ms=min(18100, D),
                      effects=[EffectApplication(effect_id="PUNCH_ZOOM",
                                                 params={"scale_end": 1.08})],
                      actual_video_evidence="12.4s~ 마무리 윙크 — punch zoom 강조"),
    ],
    overlays=[
        Overlay(overlay_id="ov_c1", produced_segment_id="ps_intro",
                overlay_type=OverlayType.CAPTION, text_content="아파트 아파트",
                style_id="CAPTION", start_ms=600, end_ms=3800,
                motion_id=MotionId.POP, font_weight=FontWeight.SEMIBOLD,
                actual_video_evidence="도입 손동작"),
        Overlay(overlay_id="ov_c2", produced_segment_id="ps_dance",
                overlay_type=OverlayType.CAPTION,
                text_content="이 챌린지 아직 안 해봤다면",
                style_id="CAPTION", start_ms=4600, end_ms=7600,
                motion_id=MotionId.FADE, font_weight=FontWeight.SEMIBOLD),
        Overlay(overlay_id="ov_t1", produced_segment_id="ps_dance",
                overlay_type=OverlayType.TEXT_2D, text_content="APT. CHALLENGE",
                style_id="TEXT_2D", start_ms=8300, end_ms=12000,
                motion_id=MotionId.POP, font_weight=FontWeight.BOLD,
                actual_video_evidence="원본 APT 텍스트 구간과 겹치지 않는 위치 필요"),
        Overlay(overlay_id="ov_cta", produced_segment_id="ps_finale",
                overlay_type=OverlayType.CAPTION, text_content="저장하고 같이 춰봐요",
                style_id="CTA_BOX", start_ms=15300, end_ms=17900,
                motion_id=MotionId.FADE, font_weight=FontWeight.BOLD),
        # SFX — intent만 출력, 실제 자산은 Resolver가 (구현 문서 20.1)
        Overlay(overlay_id="ov_sfx1", produced_segment_id="ps_dance",
                overlay_type=OverlayType.SFX, sfx_intent_id="TEXT_POP",
                start_ms=4600, end_ms=4800, audio_volume_db=-14,
                sfx_strength=SfxStrength.LIGHT),
        Overlay(overlay_id="ov_sfx2", produced_segment_id="ps_dance",
                overlay_type=OverlayType.SFX, sfx_intent_id="TEXT_POP",
                start_ms=8300, end_ms=8500, audio_volume_db=-14,
                sfx_strength=SfxStrength.LIGHT),
        Overlay(overlay_id="ov_sfx3", produced_segment_id="ps_finale",
                overlay_type=OverlayType.SFX, sfx_intent_id="CTA_APPEAR",
                start_ms=15300, end_ms=15650, audio_volume_db=-12,
                sfx_strength=SfxStrength.MEDIUM),
    ],
)

line("A. ONE_TAKE — FINAL_RENDER (SFX_ONLY)")
res_a = engine.final_render(
    FinalRenderRequest(job_id=f"render_{uuid.uuid4().hex[:6]}",
                       produced_video=produced,
                       source_mode=SourceMode.ONE_TAKE_PASSTHROUGH,
                       edit_recipe=recipe_a),
    out_path=str(OUT / "final_one_take_sfx.mp4"))
show(res_a)
if res_a.render_manifest:
    m = res_a.render_manifest
    print(f"  concat_order={m.concat_order}")
    print(f"  sfx={[(w, a['intent']) for w, a in zip(m.sfx_windows_ms, m.sfx_assets)]}")

# ─────────────────────────────────────────────────────────────────────
line("B. MULTI_CUT — 원본 3분할 → CUT_ASSEMBLY")
cut_paths = []
for i, (ss, t) in enumerate([(0, 6), (6, 6), (12, None)], start=1):
    cp = WORK / f"raw_cut_{i}.mp4"
    cmd = [FFMPEG, "-hide_banner", "-y", "-ss", str(ss)]
    if t:
        cmd += ["-t", str(t)]
    cmd += ["-i", SRC, "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(cp)]
    run(cmd)
    cut_paths.append(cp)
    print(f"  raw_cut_{i}: {probe(cp)['duration_ms']}ms")

guide = [GuideSegmentRef(guide_template_segment_id=f"gts_00{i}",
                         guide_sequence_index=i, start_ms=(i - 1) * 6000,
                         end_ms=i * 6000) for i in (1, 2, 3)]
raw_cuts = [RawCut(raw_cut_file_id=f"raw_00{i}", capture_sequence_index=i,
                   file=media_ref(f"raw_00{i}", p))
            for i, p in enumerate(cut_paths, start=1)]

engine.segmenter = MockSegmenter({
    "raw_001": {"guide_id": "gts_001", "confidence": 0.92,
                "lead_dead_ms": 280, "tail_dead_ms": 150,
                "evidence": "동작 시작 전 280ms 대기 관측"},
    "raw_002": {"guide_id": "gts_002", "confidence": 0.50,
                "lead_dead_ms": 900, "tail_dead_ms": 400,
                "evidence": "저신뢰 — 정책상 원본 유지"},
    "raw_003": {"guide_id": "gts_003", "confidence": 0.86,
                "lead_dead_ms": 200, "tail_dead_ms": 0},
})
req_cut = CutAssemblyRequest(job_id=f"cut_{uuid.uuid4().hex[:6]}",
                             shoot_session_id="shoot_demo_001",
                             guide_template_id="gt_apt_001",
                             guide_segments=guide, raw_cuts=raw_cuts)
res_b = engine.cut_assembly(req_cut)
show(res_b)
cm = res_b.cut_manifest
for it in cm.items:
    print(f"    cut#{it.capture_sequence_index} → out#{it.output_sequence_index} "
          f"{it.decision.value:14} trim[{it.trim_in_ms},{it.trim_out_ms}] "
          f"conf={it.confidence} ({it.decision_reason})")
print(f"  assembled: {cm.assembled_file.duration_ms}ms qc={cm.qc_status.value} "
      f"warnings={cm.warnings}")

line("B2. idempotency — 동일 요청 재실행(캐시 적중해야 함)")
res_b2 = engine.cut_assembly(req_cut)
print(f"  cached status={res_b2.status} 같은파일={res_b2.cut_manifest.assembled_file.path == cm.assembled_file.path}")

line("B3. 결합본 → FINAL_RENDER (SILENT)")
asm = cm.assembled_file
AD = asm.duration_ms
recipe_b = EditRecipe(
    recipe_id="recipe_demo_apt_002", produced_video_id=asm.file_id,
    final_audio_policy=FinalAudioPolicy.SILENT, audio_mix_policy_id="SILENT_V1",
    segments=[RecipeSegment(recipe_segment_id="rs_asm_001",
                            produced_segment_id="ps_asm", sequence_index=1,
                            trim_in_ms=0, trim_out_ms=AD - 40)],
    overlays=[Overlay(overlay_id="ov_b1", produced_segment_id="ps_asm",
                      overlay_type=OverlayType.CAPTION,
                      text_content="컷 3개를 순서 그대로 결합했어요",
                      style_id="CAPTION", start_ms=800, end_ms=4200,
                      motion_id=MotionId.FADE)],
)
res_b3 = engine.final_render(
    FinalRenderRequest(job_id=f"render_{uuid.uuid4().hex[:6]}",
                       produced_video=asm,
                       source_mode=SourceMode.MULTI_CUT_ASSEMBLED,
                       edit_recipe=recipe_b),
    out_path=str(OUT / "final_multicut_silent.mp4"))
show(res_b3)

# ─────────────────────────────────────────────────────────────────────
line("C. 차단 검증 — Validator가 렌더 진입을 막아야 함")
bad1 = recipe_a.model_copy(deep=True)
bad1.overlays[0].text_content = "아파트 챌린지 🔥"
r = engine.final_render(FinalRenderRequest(job_id="blocked_1", produced_video=produced,
                                           source_mode=SourceMode.ONE_TAKE_PASSTHROUGH,
                                           edit_recipe=bad1),
                        out_path=str(OUT / "_never.mp4"))
print(f"  [이모지 글리프] {r.status}: {r.error.splitlines()[-1] if r.error else ''}")

bad2 = recipe_a.model_copy(deep=True)
bad2.segments[2].effects[0].params["scale_end"] = 1.5
r = engine.final_render(FinalRenderRequest(job_id="blocked_2", produced_video=produced,
                                           source_mode=SourceMode.ONE_TAKE_PASSTHROUGH,
                                           edit_recipe=bad2),
                        out_path=str(OUT / "_never.mp4"))
print(f"  [zoom 범위 밖 ] {r.status}: {r.error.splitlines()[-1] if r.error else ''}")

bad3 = recipe_a.model_copy(deep=True)
bad3.segments = [bad3.segments[0].model_copy(update={"sequence_index": 3})] + bad3.segments[1:]
r = engine.final_render(FinalRenderRequest(job_id="blocked_3", produced_video=produced,
                                           source_mode=SourceMode.ONE_TAKE_PASSTHROUGH,
                                           edit_recipe=bad3),
                        out_path=str(OUT / "_never.mp4"))
print(f"  [순서 위반    ] {r.status}: {r.error.splitlines()[-1] if r.error else ''}")

# ─────────────────────────────────────────────────────────────────────
line("D. SFX Provider 장애 → SILENT fallback")
engine.sfx_resolver = FailingSfxResolver()
res_d = engine.final_render(
    FinalRenderRequest(job_id=f"render_{uuid.uuid4().hex[:6]}",
                       produced_video=produced,
                       source_mode=SourceMode.ONE_TAKE_PASSTHROUGH,
                       edit_recipe=recipe_a.model_copy(
                           update={"recipe_id": "recipe_demo_apt_001_fb"})),
    out_path=str(OUT / "final_one_take_fallback_silent.mp4"))
show(res_d)
if res_d.render_manifest:
    print(f"  effective_audio={res_d.render_manifest.versions.get('final_audio_policy_effective')}")
    print(f"  fallback={res_d.render_manifest.versions.get('fallback')}")

line("요약")
summary = {
    "A_one_take_sfx": {"deliverable": res_a.deliverable, "qc": res_a.qc.status.value},
    "B_cut_assembly": {"deliverable": res_b.deliverable, "qc": cm.qc_status.value,
                       "decisions": [it.decision.value for it in cm.items]},
    "B3_multicut_silent": {"deliverable": res_b3.deliverable, "qc": res_b3.qc.status.value},
    "D_fallback": {"deliverable": res_d.deliverable,
                   "effective_audio": res_d.render_manifest.versions.get(
                       "final_audio_policy_effective")},
}
print(json.dumps(summary, ensure_ascii=False, indent=2))
(OUT / "demo_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
(OUT / "render_manifest_A.json").write_text(res_a.render_manifest.model_dump_json(indent=2))
(OUT / "cut_manifest_B.json").write_text(cm.model_dump_json(indent=2))
