"""4090 환경 스모크 테스트 — 어댑터별 가용성과 실제 추론까지 확인.

    python tools/verify_gpu.py [--video path.mp4]

각 항목은 독립적으로 판정한다. 하나가 실패해도 엔진은 폴백으로 동작한다.
"""
from __future__ import annotations
import argparse, pathlib, subprocess, sys, time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for _d in (".work", "output"):
    (ROOT / _d).mkdir(exist_ok=True)   # _ensure_dirs

from reals_edit_engine.model_adapters.device import (cuda_available, gpu_info,
                                                      nvenc_available, onnx_providers)


def check(label, fn):
    t0 = time.time()
    try:
        detail = fn()
        print(f"  [ OK ] {label:34} {time.time()-t0:5.1f}s  {detail}")
        return True
    except Exception as e:
        print(f"  [FAIL] {label:34} {time.time()-t0:5.1f}s  {type(e).__name__}: "
              f"{str(e)[:120]}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default=str(ROOT / ".work" / "produced_one_take.mp4"))
    args = ap.parse_args()

    print(f"\n=== 환경 ===")
    print(f"  gpu        : {gpu_info()}")
    print(f"  onnx       : {onnx_providers()}")
    nv = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                        capture_output=True, text=True).stdout
    built = "h264_nvenc" in nv
    works = nvenc_available()          # 실제 0.1초 테스트 인코드
    print(f"  h264_nvenc : 빌드={'있음' if built else '없음'} / "
          f"실동작={'가능' if works else '불가 → CPU 인코딩 사용'}")

    video = args.video
    if not pathlib.Path(video).exists():
        print(f"\n영상 없음: {video} — 비전 검사는 건너뜁니다.")
        return

    from reals_edit_engine.model_adapters.frames import sample_frames
    frames = sample_frames(video, [(0, 4000)], per_window=2)

    print(f"\n=== 비전 어댑터 (프레임 {len(frames)}장) ===")
    from reals_edit_engine.model_adapters import vision

    def run_adapter(factory):
        ad = factory()
        try:
            return f"{len(ad.regions(frames))} regions"
        finally:
            ad.close()          # 인터프리터 종료 시 mediapipe __del__ 잡음 방지

    check("mediapipe/face", lambda: run_adapter(vision.MediaPipeFaceAdapter))
    check("mediapipe/pose", lambda: run_adapter(vision.MediaPipePoseAdapter))
    check("mediapipe/selfie", lambda: run_adapter(vision.MediaPipeSegmentAdapter))
    check("mediapipe/efficientdet", lambda: run_adapter(vision.MediaPipeObjectAdapter))
    check("rapidocr/pp-ocr", lambda: run_adapter(vision.RapidOcrTextAdapter))

    if cuda_available():
        from reals_edit_engine.model_adapters import vision_gpu
        check("sam3.1/concept", lambda: run_adapter(vision_gpu.Sam3Adapter))
        check("ultralytics/yolo", lambda: run_adapter(vision_gpu.YoloDetectorAdapter))
    else:
        print("  [SKIP] sam3.1 / yolo — CUDA 없음")

    print(f"\n=== READ-1 세그멘터 ===")
    from reals_edit_engine.model_adapters.quality import (analyze_motion,
                                                           dead_edges_ms,
                                                           quality_confidence)
    def motion():
        mp_ = analyze_motion(video)
        return (f"dead={dead_edges_ms(mp_)} conf={quality_confidence(mp_)} "
                f"shake={mp_.shake:.4f}")
    check("opencv/motion-quality", motion)

    if cuda_available():
        from reals_edit_engine.model_adapters.semantic import LocalVlmSegmenter
        def vlm():
            s = LocalVlmSegmenter()
            s._lazy()
            return f"loaded {s.model_id}"
        check("qwen3-vl/local (load only)", vlm)
    else:
        print("  [SKIP] qwen3-vl — CUDA 없음")

    print(f"\n=== 렌더 프로파일 ===")
    from reals_edit_engine.registries import Registries
    from reals_edit_engine.ffmpeg_graph import video_encode_args
    reg = Registries(ROOT)
    for pid in ("INSTAGRAM_REELS_V1", "INSTAGRAM_REELS_NVENC_V1"):
        print(f"  {pid:28} {' '.join(video_encode_args(reg.render_profile(pid)))}")
    check("PRETENDARD 해시 검증",
          lambda: reg.resolve_font("PRETENDARD", "SEMIBOLD")["ass_family"])


if __name__ == "__main__":
    main()
