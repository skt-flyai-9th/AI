"""실제 모델 스택 검증 — Mock 없이 CUT_ASSEMBLY + FINAL_RENDER.

이 컨테이너(CPU)에서 돌지만 코드 경로는 4090과 동일하다.
CUDA가 있으면 SAM 3.1 / YOLO / Qwen3-VL / NVENC로 자동 승격된다.
"""
import argparse
import json, pathlib, sys, time, uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _d in (".work", "output"):
    (ROOT / _d).mkdir(exist_ok=True)   # _ensure_dirs
sys.path.insert(0, str(ROOT / "demo"))

from sarils_edit_engine import VideoEditEngine
from sarils_edit_engine.contracts import (CutAssemblyRequest, EditRecipe,
                                          FinalAudioPolicy, FinalRenderRequest,
                                          GuideSegmentRef, MotionId, Overlay,
                                          OverlayType, RawCut, RecipeSegment,
                                          SourceMode)
from sarils_edit_engine.media import FFMPEG, media_ref, normalize, run
from sarils_edit_engine.model_adapters.avoid_map import VisionAvoidMapProvider
from sarils_edit_engine.model_adapters.device import cuda_available, nvenc_available, gpu_info
from sarils_edit_engine.model_adapters.semantic import HybridSemanticSegmenter
from sarils_edit_engine.sfx import MockSynthSfxResolver
from recipes import build_recipe_a

parser = argparse.ArgumentParser()
parser.add_argument("--video", help="입력 촬영 영상. 지정하면 테스트용 3개 컷을 자동 생성")
args = parser.parse_args()

GPU = cuda_available()
print(f"=== device: {gpu_info()} nvenc={nvenc_available()} ===")
NV = "_NVENC_V1" if nvenc_available() else "_V1"
render_profile = f"INSTAGRAM_REELS{NV}"
inter_profile = ("INTERMEDIATE_NVENC_V1" if GPU else "INTERMEDIATE_VERTICAL_V1")

segmenter = HybridSemanticSegmenter.default(prefer="auto")
avoid = VisionAvoidMapProvider.default_stack(prefer_gpu=True, frames_per_window=2)
engine = VideoEditEngine(ROOT, segmenter=segmenter,
                         sfx_resolver=MockSynthSfxResolver(),
                         avoid_provider=avoid)

# --video 지정 시 테스트 입력 자동 준비
if args.video:
    src = pathlib.Path(args.video).expanduser().resolve()
    if not src.exists():
        sys.exit(f"입력 영상 없음: {src}")

    produced_path = ROOT / ".work" / "produced_one_take.mp4"
    print(f"입력 영상 준비: {src}")

    normalize(
        src,
        produced_path,
        engine.reg.render_profile(inter_profile),
        keep_audio=True,
    )

    produced_ref = media_ref("prod_prepare", produced_path)
    duration_ms = produced_ref.duration_ms

    # 기존 테스트 컷 제거
    for old_cut in (ROOT / ".work").glob("raw_cut_*.mp4"):
        old_cut.unlink()

    # 전체 영상을 시간순으로 3등분 — 테스트용, 순서 재배열 없음
    bounds = [0, duration_ms // 3, (duration_ms * 2) // 3, duration_ms]

    for i in range(3):
        start_s = bounds[i] / 1000
        duration_s = (bounds[i + 1] - bounds[i]) / 1000
        dst = ROOT / ".work" / f"raw_cut_{i + 1}.mp4"

        run([
            FFMPEG, "-hide_banner", "-y",
            "-ss", f"{start_s:.3f}",
            "-i", str(produced_path),
            "-t", f"{duration_s:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            str(dst),
        ])

    print(f"테스트 컷 자동 생성 완료: 3개 ({duration_ms}ms)")

# ── 1. CUT_ASSEMBLY (실제 모션·품질 분석) ───────────────────────────
cut_paths = sorted((ROOT / ".work").glob("raw_cut_*.mp4"))
raw_cuts = [RawCut(raw_cut_file_id=f"raw_00{i}", capture_sequence_index=i,
                   file=media_ref(f"raw_00{i}", p))
            for i, p in enumerate(cut_paths, start=1)]
guide = [GuideSegmentRef(guide_template_segment_id=f"gts_00{i}",
                         guide_sequence_index=i, start_ms=(i - 1) * 6000,
                         end_ms=i * 6000,
                         scene_summary=s)
         for i, s in enumerate(["도입 포즈와 첫 손동작", "APT 반복 안무 본절",
                                "마무리 포즈와 윙크"], start=1)]

print("\n=== CUT_ASSEMBLY (real segmenter) ===")
t0 = time.time()
res_cut = engine.cut_assembly(CutAssemblyRequest(
    job_id=f"cut_{uuid.uuid4().hex[:6]}", shoot_session_id="shoot_real_001",
    guide_template_id="gt_apt_001", guide_segments=guide, raw_cuts=raw_cuts,
    output_profile_id=inter_profile))
print(f"  {time.time()-t0:.1f}s  status={res_cut.status} deliverable={res_cut.deliverable}")
if res_cut.error:
    print("  error:", res_cut.error[:300])
cm = res_cut.cut_manifest
if cm:
    print("  segmenter warnings:", segmenter.warnings)
    for it in cm.items:
        print(f"    cut#{it.capture_sequence_index} → out#{it.output_sequence_index} "
              f"{it.decision.value:14} trim[{it.trim_in_ms},{it.trim_out_ms}] "
              f"conf={it.confidence}")
    print(f"  assembled {cm.assembled_file.duration_ms}ms qc={cm.qc_status.value}")

# ── 2. FINAL_RENDER (실제 Avoid Map) ────────────────────────────────
print("\n=== FINAL_RENDER (real vision avoid map) ===")
produced = media_ref("prod_one_take_001", ROOT / ".work" / "produced_one_take.mp4")
recipe = build_recipe_a(produced.duration_ms).model_copy(
    update={"recipe_id": "recipe_real_001", "render_profile_id": render_profile})
t0 = time.time()
res = engine.final_render(
    FinalRenderRequest(job_id=f"render_{uuid.uuid4().hex[:6]}",
                       produced_video=produced,
                       source_mode=SourceMode.ONE_TAKE_PASSTHROUGH,
                       edit_recipe=recipe),
    out_path=str(ROOT / "output" / "final_real_stack.mp4"))
print(f"  {time.time()-t0:.1f}s  status={res.status} deliverable={res.deliverable}")
if res.error:
    print("  error:", res.error[:400])
print("  vision used:", avoid.used)
print("  vision warnings:", avoid.warnings)
for c in (res.qc.checks if res.qc else []):
    print(f"    [{c.status.value:4}] {c.check_id}: {c.detail}")
if res.render_manifest:
    (ROOT / "output" / "render_manifest_real.json").write_text(
        res.render_manifest.model_dump_json(indent=2))
