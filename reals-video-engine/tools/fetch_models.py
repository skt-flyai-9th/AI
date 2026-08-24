"""렌더 폰트와 MediaPipe 모델 다운로드 — Windows/WSL/Linux 공용."""
import hashlib
import pathlib
import tempfile
import urllib.request
import zipfile

BASE = "https://storage.googleapis.com/mediapipe-models"
MODEL_FILES = {
    "face_detector.tflite":
        f"{BASE}/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
    "selfie_multiclass.tflite":
        f"{BASE}/image_segmenter/selfie_multiclass_256x256/float32/1/selfie_multiclass_256x256.tflite",
    "pose_landmarker_full.task":
        f"{BASE}/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task",
    "efficientdet_lite2.tflite":
        f"{BASE}/object_detector/efficientdet_lite2/float32/1/efficientdet_lite2.tflite",
}

ROOT = pathlib.Path(__file__).resolve().parents[1]
model_dst = ROOT / "assets" / "models"
model_dst.mkdir(parents=True, exist_ok=True)
for name, url in MODEL_FILES.items():
    out = model_dst / name
    if out.exists() and out.stat().st_size > 10_000:
        print(f"  skip  {name} (이미 있음)")
        continue
    print(f"  →     {name}")
    urllib.request.urlretrieve(url, out)

font_dst = ROOT / "assets" / "fonts"
font_dst.mkdir(parents=True, exist_ok=True)
font_names = (
    "Pretendard-Regular.otf",
    "Pretendard-SemiBold.otf",
    "Pretendard-Bold.otf",
)
fonts_ready = all(
    (font_dst / name).exists() and (font_dst / name).stat().st_size > 1_000_000
    for name in font_names
)
if fonts_ready:
    print("  skip  Pretendard 1.3.9 (이미 있음)")
else:
    font_url = (
        "https://github.com/orioncactus/pretendard/releases/download/"
        "v1.3.9/Pretendard-1.3.9.zip"
    )
    expected_zip_sha256 = (
        "04be351a74d6bf7d60c480a3087e51d185485d35a52023142af1df19eb8c428a"
    )
    print("  →     Pretendard 1.3.9")
    with tempfile.TemporaryDirectory() as tmp:
        archive = pathlib.Path(tmp) / "Pretendard-1.3.9.zip"
        urllib.request.urlretrieve(font_url, archive)
        actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        if actual != expected_zip_sha256:
            raise RuntimeError(f"Pretendard archive SHA-256 mismatch: {actual}")
        with zipfile.ZipFile(archive) as bundle:
            for name in font_names:
                with bundle.open(f"public/static/{name}") as source:
                    (font_dst / name).write_bytes(source.read())

print("완료. SAM 3.1 / YOLO11 / Qwen3-VL 가중치는 GPU 최초 실행 시 자동 다운로드됩니다.")
