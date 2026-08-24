"""GPU 전용 고성능 어댑터 — RTX 4090(24GB) 기준.

이 모듈은 CUDA 환경에서만 실제 로드된다. import 자체는 항상 성공하고
로드 시점에 의존성이 없으면 명확한 예외를 낸다 → 상위에서 CPU 어댑터로 폴백.

VRAM 예산(4090 24GB):
  SAM 3.1 (base+)      ~4-6 GB
  YOLO26-l             ~2 GB
  Qwen3-VL 8B (AWQ)    ~10-12 GB
  PP-OCR (onnx-gpu)    ~1 GB
동시 상주는 피하고 단계별 lazy load → close() 순으로 돌린다.
"""
from __future__ import annotations
import pathlib
from typing import Iterable

import cv2
import numpy as np

from ..contracts import AvoidRegion
from .device import cuda_available, device_str, free_cuda
from .frames import Frame
from .vision import VisionAdapter, _bbox_from_mask


class AdapterUnavailable(RuntimeError):
    """의존성·가중치·GPU 부재 — 상위에서 폴백 신호로 사용."""


# ── SAM 3.1: 개념(텍스트) 프롬프트 기반 분할·추적 ────────────────────
class Sam3Adapter(VisionAdapter):
    """SAM 3.1은 텍스트 개념 프롬프트를 받는다 → 상품/음식/사람을 바로 지정.

    설치: requirements-gpu.txt의 고정된 SAM 3 커밋
    가중치: HF facebook/sam3.1 (승인 계정의 `hf auth login` 필요)
    """
    name = "sam3.1/concept-prompt"
    DEFAULT_PROMPTS = ["person", "hand", "food", "drink", "product", "sign"]
    PRIORITY = {"person": 80, "hand": 95, "food": 95, "drink": 95,
                "product": 100, "sign": 85}

    def __init__(self, prompts: list[str] | None = None,
                 checkpoint: str = "facebook/sam3.1", min_score: float = 0.45):
        self.prompts = prompts or self.DEFAULT_PROMPTS
        self.checkpoint = checkpoint
        self.min_score = min_score
        self._m = None

    def _lazy(self):
        if self._m is None:
            if not cuda_available():
                raise AdapterUnavailable("SAM 3.1은 CUDA 필요")
            try:
                from sam3.model.sam3_image_processor import Sam3Processor
                from sam3.model_builder import (build_sam3_image_model,
                                                download_ckpt_from_hf)

                if self.checkpoint in {"facebook/sam3.1", "sam3.1"}:
                    checkpoint_path = download_ckpt_from_hf(version="sam3.1")
                elif self.checkpoint in {"facebook/sam3", "sam3"}:
                    checkpoint_path = download_ckpt_from_hf(version="sam3")
                else:
                    checkpoint_path = str(
                        pathlib.Path(self.checkpoint).expanduser().resolve()
                    )

                model = build_sam3_image_model(
                    checkpoint_path=checkpoint_path,
                    load_from_HF=False,
                )
                self._m = Sam3Processor(
                    model, device="cuda", confidence_threshold=self.min_score
                )
            except Exception as e:
                raise AdapterUnavailable(f"SAM 3.1 로드 실패: {e}") from e
        return self._m

    def regions(self, frames: Iterable[Frame]) -> list[AvoidRegion]:
        from PIL import Image
        import torch

        m = self._lazy()
        out: list[AvoidRegion] = []
        for f in frames:
            rgb = cv2.cvtColor(f.bgr, cv2.COLOR_BGR2RGB)
            # SAM 3.x's official CUDA examples run inference under BF16
            # autocast. Without it, the BF16 activations can meet FP32 linear
            # weights and fail with a dtype mismatch on recent checkpoints.
            with torch.inference_mode(), torch.autocast(
                "cuda", dtype=torch.bfloat16
            ):
                state = m.set_image(Image.fromarray(rgb))
                for label in self.prompts:
                    res = m.set_text_prompt(prompt=label, state=state)
                    masks = res["masks"].detach().float().cpu().numpy()
                    scores = res["scores"].detach().float().cpu().numpy()
                    for mask, score in zip(masks, scores):
                        score = float(score)
                        bb = _bbox_from_mask(
                            np.asarray(mask).squeeze() > 0.5, f.scale, pad=8
                        )
                        if not bb:
                            continue
                        x, y, w, h = bb
                        out.append(AvoidRegion(
                            x=x, y=y, w=w, h=h,
                            priority=self.PRIORITY.get(label, 85),
                            label=f"SAM:{label.upper()}",
                            start_ms=f.t_ms - 250, end_ms=f.t_ms + 250))
        return out

    def close(self):
        if self._m is not None:
            self._m.model = None
        self._m = None
        free_cuda()


# ── YOLO (Ultralytics) — 상품·객체 정밀 bbox ─────────────────────────
class YoloDetectorAdapter(VisionAdapter):
    """설치: pip install ultralytics. 가중치는 최초 실행 시 자동 다운로드."""
    name = "ultralytics/yolo"

    def __init__(self, weights: str = "yolo11l.pt", min_score: float = 0.35):
        self.weights = weights
        self.min_score = min_score
        self._m = None

    def _lazy(self):
        if self._m is None:
            try:
                from ultralytics import YOLO
            except Exception as e:
                raise AdapterUnavailable(f"ultralytics 미설치: {e}")
            self._m = YOLO(self.weights)
        return self._m

    BATCH = 4          # 노트북 GPU 기준 안전 배치

    def regions(self, frames):
        from .vision import FOOD_LABELS
        frames = list(frames)
        if not frames:
            return []
        m = self._lazy()
        results = []
        for i in range(0, len(frames), self.BATCH):
            chunk = [f.bgr for f in frames[i:i + self.BATCH]]
            results.extend(m.predict(chunk, conf=self.min_score,
                                     device=device_str(), verbose=False))
            free_cuda()
        out = []
        for f, r in zip(frames, results):
            names = r.names
            for b in r.boxes:
                cat = names[int(b.cls)]
                if cat == "person":
                    continue
                x1, y1, x2, y2 = [float(v) * f.scale for v in b.xyxy[0].tolist()]
                prio, label = ((95, f"FOOD:{cat}") if cat in FOOD_LABELS
                               else (85, f"OBJECT:{cat}"))
                out.append(AvoidRegion(x=int(x1), y=int(y1),
                                       w=int(x2 - x1), h=int(y2 - y1),
                                       priority=prio, label=label,
                                       start_ms=f.t_ms - 250, end_ms=f.t_ms + 250))
        return out

    def close(self):
        self._m = None
        free_cuda()
