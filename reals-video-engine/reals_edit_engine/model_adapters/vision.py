"""실제 비전 어댑터 — Avoid Map 생성용 (구현 문서 16.3·18.4).

우선순위 체계(18.4)에 맞춰 각 어댑터가 AvoidRegion을 반환한다.
  FACE 100 / PRIMARY_PRODUCT 100 / FULL_BODY 100 / FOOD·DRINK 95
  HANDS·FEET 95 / TEXT_REGION 90 / LOGO 85 / PERSON_BODY 80

모든 어댑터는 lazy load + close() 지원. CUDA 있으면 GPU, 없으면 CPU.
"""
from __future__ import annotations
import pathlib
from typing import Iterable

import cv2
import numpy as np

from ..contracts import AvoidRegion
from .device import cuda_available, device_str, onnx_providers
from .frames import Frame

MODELS = pathlib.Path(__file__).resolve().parents[2] / "assets" / "models"

# selfie_multiclass 라벨 → (우선순위, 라벨명)
SELFIE_CLASSES = {1: (100, "HAIR"), 2: (80, "BODY_SKIN"), 3: (100, "FACE_SKIN"),
                  4: (80, "CLOTHES"), 5: (80, "PERSON_BODY")}


def _bbox_from_mask(mask: np.ndarray, scale: float, pad: int = 6):
    mask = np.squeeze(np.asarray(mask))
    if mask.ndim > 2:                     # (H,W,1) 또는 (1,H,W) 방어
        mask = mask.reshape(mask.shape[-2:]) if mask.shape[0] == 1 else mask[..., 0]
    ys, xs = np.nonzero(mask)
    if xs.size == 0:
        return None
    x0, x1 = int(xs.min() * scale) - pad, int(xs.max() * scale) + pad
    y0, y1 = int(ys.min() * scale) - pad, int(ys.max() * scale) + pad
    return max(0, x0), max(0, y0), x1 - max(0, x0), y1 - max(0, y0)


class VisionAdapter:
    name = "abstract"
    def regions(self, frames: Iterable[Frame]) -> list[AvoidRegion]:
        raise NotImplementedError
    def close(self):
        pass


# ── 1. 얼굴 (MediaPipe BlazeFace) ────────────────────────────────────
class MediaPipeFaceAdapter(VisionAdapter):
    name = "mediapipe/face_detector"

    def __init__(self, min_conf: float = 0.35, model: str = "face_detector.tflite"):
        self._min_conf = min_conf
        self._model = MODELS / model
        self._det = None

    def _lazy(self):
        if self._det is None:
            from mediapipe.tasks import python as mpp
            from mediapipe.tasks.python import vision as mpv
            self._det = mpv.FaceDetector.create_from_options(mpv.FaceDetectorOptions(
                base_options=mpp.BaseOptions(model_asset_path=str(self._model)),
                min_detection_confidence=self._min_conf,
                running_mode=mpv.RunningMode.IMAGE))
        return self._det

    def regions(self, frames):
        import mediapipe as mp
        det = self._lazy()
        out = []
        for f in frames:
            rgb = cv2.cvtColor(f.bgr, cv2.COLOR_BGR2RGB)
            res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            for d in res.detections:
                b = d.bounding_box
                # 얼굴은 헤어라인·턱까지 여유를 준다
                x = int(b.origin_x * f.scale) - 18
                y = int(b.origin_y * f.scale) - 40
                w = int(b.width * f.scale) + 36
                h = int(b.height * f.scale) + 70
                out.append(AvoidRegion(x=max(0, x), y=max(0, y), w=w, h=h,
                                       priority=100, label="FACE",
                                       start_ms=f.t_ms - 250, end_ms=f.t_ms + 250))
        return out

    def close(self):
        if self._det is not None:
            self._det.close(); self._det = None


# ── 2. 인물 세그멘테이션 (MediaPipe selfie multiclass) ────────────────
class MediaPipeSegmentAdapter(VisionAdapter):
    """SAM 3.1이 없을 때의 기본 인물 마스크. 있으면 Sam3Adapter가 대체."""
    name = "mediapipe/selfie_multiclass"

    def __init__(self, model: str = "selfie_multiclass.tflite"):
        self._model = MODELS / model
        self._seg = None

    def _lazy(self):
        if self._seg is None:
            from mediapipe.tasks import python as mpp
            from mediapipe.tasks.python import vision as mpv
            self._seg = mpv.ImageSegmenter.create_from_options(mpv.ImageSegmenterOptions(
                base_options=mpp.BaseOptions(model_asset_path=str(self._model)),
                running_mode=mpv.RunningMode.IMAGE,
                output_category_mask=True))
        return self._seg

    def regions(self, frames):
        import mediapipe as mp
        seg = self._lazy()
        out = []
        for f in frames:
            rgb = cv2.cvtColor(f.bgr, cv2.COLOR_BGR2RGB)
            res = seg.segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            cat = np.squeeze(np.asarray(res.category_mask.numpy_view()))
            if cat.ndim > 2:
                cat = cat[..., 0]
            person = np.isin(cat, list(SELFIE_CLASSES.keys()))
            if person.mean() < 0.01:
                continue
            bb = _bbox_from_mask(person, f.scale, pad=10)
            if bb:
                x, y, w, h = bb
                out.append(AvoidRegion(x=x, y=y, w=w, h=h, priority=80,
                                       label="PERSON_BODY",
                                       start_ms=f.t_ms - 250, end_ms=f.t_ms + 250))
        return out

    def close(self):
        if self._seg is not None:
            self._seg.close(); self._seg = None


# ── 3. 손·발 (MediaPipe Pose landmarks) ──────────────────────────────
class MediaPipePoseAdapter(VisionAdapter):
    name = "mediapipe/pose_landmarker_full"
    HAND_IDX = [15, 16, 17, 18, 19, 20, 21, 22]
    FOOT_IDX = [27, 28, 29, 30, 31, 32]

    def __init__(self, model: str = "pose_landmarker_full.task"):
        self._model = MODELS / model
        self._pose = None

    def _lazy(self):
        if self._pose is None:
            from mediapipe.tasks import python as mpp
            from mediapipe.tasks.python import vision as mpv
            self._pose = mpv.PoseLandmarker.create_from_options(mpv.PoseLandmarkerOptions(
                base_options=mpp.BaseOptions(model_asset_path=str(self._model)),
                running_mode=mpv.RunningMode.IMAGE, num_poses=1))
        return self._pose

    def regions(self, frames):
        import mediapipe as mp
        pose = self._lazy()
        out = []
        for f in frames:
            rgb = cv2.cvtColor(f.bgr, cv2.COLOR_BGR2RGB)
            res = pose.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            if not res.pose_landmarks:
                continue
            lm = res.pose_landmarks[0]
            ph, pw = f.bgr.shape[:2]
            for idxs, label in ((self.HAND_IDX, "HANDS"), (self.FOOT_IDX, "FEET")):
                pts = [(lm[i].x * pw * f.scale, lm[i].y * ph * f.scale)
                       for i in idxs if i < len(lm) and lm[i].visibility > 0.5]
                if len(pts) < 2:
                    continue
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                x0, y0 = int(min(xs)) - 40, int(min(ys)) - 40
                out.append(AvoidRegion(x=max(0, x0), y=max(0, y0),
                                       w=int(max(xs) - min(xs)) + 80,
                                       h=int(max(ys) - min(ys)) + 80,
                                       priority=95, label=label,
                                       start_ms=f.t_ms - 250, end_ms=f.t_ms + 250))
        return out

    def close(self):
        if self._pose is not None:
            self._pose.close(); self._pose = None


# ── 4. 화면 글자 (PP-OCR ONNX — GPU provider 자동) ───────────────────
class RapidOcrTextAdapter(VisionAdapter):
    """기존 자막·메뉴판·간판 회피. det만 쓰고 인식 결과는 근거로만 남긴다."""
    name = "rapidocr/pp-ocr-det"

    def __init__(self, min_score: float = 0.5):
        self._min_score = min_score
        self._ocr = None

    def _lazy(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            use_cuda = "CUDAExecutionProvider" in onnx_providers()
            try:
                self._ocr = RapidOCR(det_use_cuda=use_cuda, cls_use_cuda=use_cuda,
                                     rec_use_cuda=use_cuda)
            except TypeError:
                self._ocr = RapidOCR()
        return self._ocr

    def close(self):
        self._ocr = None

    def regions(self, frames):
        ocr = self._lazy()
        out = []
        for f in frames:
            res, _ = ocr(f.bgr)
            for item in (res or []):
                box, text, score = item[0], item[1], float(item[2])
                if score < self._min_score:
                    continue
                xs = [p[0] * f.scale for p in box]
                ys = [p[1] * f.scale for p in box]
                x0, y0 = int(min(xs)) - 12, int(min(ys)) - 12
                out.append(AvoidRegion(x=max(0, x0), y=max(0, y0),
                                       w=int(max(xs) - min(xs)) + 24,
                                       h=int(max(ys) - min(ys)) + 24,
                                       priority=90, label=f"TEXT_REGION:{text[:12]}",
                                       start_ms=f.t_ms - 400, end_ms=f.t_ms + 400))
        return out


# ── 5. 상품·객체 (MediaPipe EfficientDet / YOLO 대체 가능) ────────────
FOOD_LABELS = {"pizza", "donut", "cake", "sandwich", "hot dog", "bowl", "cup",
               "wine glass", "banana", "apple", "orange", "broccoli", "carrot",
               "bottle", "fork", "knife", "spoon"}


class MediaPipeObjectAdapter(VisionAdapter):
    name = "mediapipe/efficientdet_lite2"

    def __init__(self, model: str = "efficientdet_lite2.tflite",
                 min_score: float = 0.35):
        self._model = MODELS / model
        self._min_score = min_score
        self._det = None

    def _lazy(self):
        if self._det is None:
            from mediapipe.tasks import python as mpp
            from mediapipe.tasks.python import vision as mpv
            self._det = mpv.ObjectDetector.create_from_options(mpv.ObjectDetectorOptions(
                base_options=mpp.BaseOptions(model_asset_path=str(self._model)),
                running_mode=mpv.RunningMode.IMAGE,
                score_threshold=self._min_score))
        return self._det

    def regions(self, frames):
        import mediapipe as mp
        det = self._lazy()
        out = []
        for f in frames:
            rgb = cv2.cvtColor(f.bgr, cv2.COLOR_BGR2RGB)
            res = det.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            for d in res.detections:
                cat = d.categories[0].category_name if d.categories else ""
                if cat == "person":
                    continue                       # 인물은 세그멘터가 담당
                prio, label = ((95, f"FOOD:{cat}") if cat in FOOD_LABELS
                               else (85, f"OBJECT:{cat}"))
                b = d.bounding_box
                out.append(AvoidRegion(x=int(b.origin_x * f.scale),
                                       y=int(b.origin_y * f.scale),
                                       w=int(b.width * f.scale),
                                       h=int(b.height * f.scale),
                                       priority=prio, label=label,
                                       start_ms=f.t_ms - 250, end_ms=f.t_ms + 250))
        return out

    def close(self):
        if self._det is not None:
            self._det.close(); self._det = None
