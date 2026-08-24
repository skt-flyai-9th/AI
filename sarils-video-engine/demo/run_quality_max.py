"""품질 최대 렌더 — HQ 정규화(zscale) + 실비전 스택 + 스타일 v2 + HQ 프로파일."""
import pathlib, sys, time, uuid
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _d in (".work", "output"):
    (ROOT / _d).mkdir(exist_ok=True)   # _ensure_dirs
sys.path.insert(0, str(ROOT / "demo"))

from sarils_edit_engine import VideoEditEngine
from sarils_edit_engine.contracts import (ColorTone, EditRecipe, EffectApplication,
                                          FinalAudioPolicy, FinalRenderRequest,
                                          FontWeight, MotionId, Overlay, OverlayType,
                                          RecipeSegment, SourceMode, SfxStrength,
                                          TransitionId)
from sarils_edit_engine.media import FFMPEG, media_ref, run
from sarils_edit_engine.model_adapters.avoid_map import VisionAvoidMapProvider
from sarils_edit_engine.model_adapters.device import cuda_available, nvenc_available
from sarils_edit_engine.sfx import MockSynthSfxResolver

import argparse
_ap = argparse.ArgumentParser()
_ap.add_argument("--video", default="/root/.claude/uploads/a3ed80de-7cc6-5480-8c81-f078f68e9b03/3c1f5a94-1787520774710_20260823213132.4569313.mp4",
                 help="입력 촬영 영상 (mp4)")
_ap.add_argument("--frames", type=int, default=4,
                 help="자막 노출창당 분석 프레임 수 (기본 4, 메모리 부족 시 2)")
_ap.add_argument("--no-yolo", action="store_true",
                 help="YOLO 제외 (메모리 절약, EfficientDet로 대체)")
_ap.add_argument("--no-nvenc", action="store_true",
                 help="NVENC 대신 CPU(libx264) 인코딩 — NVENC 문제 진단용")
_args = _ap.parse_args()
SRC = _args.video
if not pathlib.Path(SRC).exists():
    sys.exit(f"입력 영상 없음: {SRC} — --video 로 지정하세요")
HQ = ROOT / ".work" / "produced_hq.mp4"

# ── HQ 정규화: zscale(lanczos) + 약한 denoise — 428p 소스의 최대 품질 업스케일 ──
if not HQ.exists():
    t0 = time.time()
    run([FFMPEG, "-hide_banner", "-y", "-i", SRC, "-vf",
         "zscale=w=1080:h=1920:filter=lanczos:param_a=3,crop=1080:1920,"
         "hqdn3d=1.2:1.0:3:3,setsar=1,fps=30,format=yuv420p",
         "-c:v", "libx264", "-crf", "16", "-preset", "medium",
         "-profile:v", "high", "-level", "4.0", "-g", "60",
         "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", "-map_metadata", "-1", str(HQ)], timeout=900)
    print(f"HQ normalize: {time.time()-t0:.0f}s")

produced = media_ref("prod_hq_001", HQ)
D = produced.duration_ms
NV = nvenc_available() and not _args.no_nvenc
profile = "INSTAGRAM_REELS_HQ_NVENC_V1" if NV else "INSTAGRAM_REELS_HQ_V1"
print(f"encoder: {'h264_nvenc' if NV else 'libx264 (CPU)'}", flush=True)

recipe = EditRecipe(
    recipe_id="recipe_quality_max_001", produced_video_id="prod_hq_001",
    final_audio_policy=FinalAudioPolicy.SFX_ONLY, audio_mix_policy_id="SFX_ONLY_V1",
    render_profile_id=profile,
    segments=[
        RecipeSegment(recipe_segment_id="rs_001", produced_segment_id="ps_intro",
                      sequence_index=1, trim_in_ms=300, trim_out_ms=4200,
                      color_tone=ColorTone.VIVID,
                      actual_video_evidence="0.3~4.2s 도입 — 대기 300ms 제거"),
        RecipeSegment(recipe_segment_id="rs_002", produced_segment_id="ps_dance",
                      sequence_index=2, trim_in_ms=4200, trim_out_ms=12400,
                      color_tone=ColorTone.VIVID,
                      transition_id=TransitionId.FLASH_WHITE,
                      actual_video_evidence="본절 진입 비트에 화이트 플래시"),
        RecipeSegment(recipe_segment_id="rs_003", produced_segment_id="ps_finale",
                      sequence_index=3, trim_in_ms=12400, trim_out_ms=min(18100, D),
                      color_tone=ColorTone.VIVID,
                      transition_id=TransitionId.FLASH_WHITE,
                      effects=[EffectApplication(effect_id="SMOOTH_ZOOM",
                                                 params={"scale_end": 1.10})],
                      actual_video_evidence="피날레 — 연속 줌 램프(RAM<10GB면 punch-in 강등)"),
    ],
    overlays=[
        Overlay(overlay_id="ov_hook", produced_segment_id="ps_intro",
                overlay_type=OverlayType.CAPTION, style_id="HOOK",
                text_content="아파트 아파트", start_ms=600, end_ms=3800,
                motion_id=MotionId.POP, font_weight=FontWeight.BOLD),
        Overlay(overlay_id="ov_c2", produced_segment_id="ps_dance",
                overlay_type=OverlayType.CAPTION, style_id="CAPTION",
                text_content="이 챌린지 아직 안 해봤다면", start_ms=4650, end_ms=7600,
                motion_id=MotionId.POP, font_weight=FontWeight.SEMIBOLD),
        Overlay(overlay_id="ov_t1", produced_segment_id="ps_dance",
                overlay_type=OverlayType.TEXT_2D, style_id="TEXT_2D",
                text_content="APT. CHALLENGE", start_ms=8300, end_ms=12000,
                motion_id=MotionId.POP, font_weight=FontWeight.BOLD),
        Overlay(overlay_id="ov_cta", produced_segment_id="ps_finale",
                overlay_type=OverlayType.CAPTION, style_id="CTA_BOX",
                text_content="저장하고 같이 춰봐요", start_ms=15300, end_ms=17900,
                motion_id=MotionId.FADE, font_weight=FontWeight.BOLD),
        Overlay(overlay_id="ov_sfx1", produced_segment_id="ps_dance",
                overlay_type=OverlayType.SFX, sfx_intent_id="FAST_TRANSITION",
                start_ms=4200, end_ms=4400, audio_volume_db=-16,
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

avoid = VisionAvoidMapProvider.default_stack(
    prefer_gpu=not _args.no_yolo, frames_per_window=_args.frames)
print(f"vision stack: {[a.name for a in avoid.adapters]} "
      f"(frames/window={_args.frames})", flush=True)
engine = VideoEditEngine(ROOT, sfx_resolver=MockSynthSfxResolver(),
                         avoid_provider=avoid)
print("FINAL_RENDER 시작 ...", flush=True)
t0 = time.time()
res = engine.final_render(
    FinalRenderRequest(job_id=f"render_{uuid.uuid4().hex[:6]}",
                       produced_video=produced,
                       source_mode=SourceMode.ONE_TAKE_PASSTHROUGH,
                       edit_recipe=recipe),
    out_path=str(ROOT / "output" / "final_quality_max.mp4"))
print(f"\nFINAL_RENDER {time.time()-t0:.0f}s  status={res.status} "
      f"deliverable={res.deliverable}  profile={profile}")
if res.error:
    print("ERROR:", res.error[:500])
print("vision used:", avoid.used)
print("vision warnings:", avoid.warnings[:3])
for c in (res.qc.checks if res.qc else []):
    print(f"  [{c.status.value:4}] {c.check_id}: {c.detail}")
if res.render_manifest:
    v = res.render_manifest.versions
    if v.get("capability_fallbacks"):
        print("capability fallback:", v["capability_fallbacks"])
    (ROOT / "output" / "render_manifest_quality.json").write_text(
        res.render_manifest.model_dump_json(indent=2))
