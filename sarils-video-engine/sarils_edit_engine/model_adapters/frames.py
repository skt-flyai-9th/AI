"""프레임 샘플링 — 모델 호출 비용을 통제하는 공통 유틸 (구현 문서 29.3).

Preview 해상도에서 계산하고 최종 좌표는 스케일 변환한다.
"""
from __future__ import annotations
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Frame:
    t_ms: int
    bgr: np.ndarray
    scale: float          # preview→canvas 배율


def sample_frames(video_path: str, windows_ms: list[tuple[int, int]],
                  per_window: int = 3, preview_long_side: int = 640) -> list[Frame]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"영상 열기 실패: {video_path}")
    total_ms = (cap.get(cv2.CAP_PROP_FRAME_COUNT) /
                max(cap.get(cv2.CAP_PROP_FPS), 1e-6)) * 1000
    targets: list[int] = []
    for w0, w1 in windows_ms:
        w0, w1 = max(0, w0), min(int(total_ms) - 1, w1)
        if w1 <= w0:
            continue
        for k in range(per_window):
            targets.append(int(w0 + (w1 - w0) * (k + 0.5) / per_window))
    out: list[Frame] = []
    for t in sorted(set(targets)):
        cap.set(cv2.CAP_PROP_POS_MSEC, t)
        ok, img = cap.read()
        if not ok:
            continue
        h, w = img.shape[:2]
        s = preview_long_side / max(h, w)
        if s < 1.0:
            img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        else:
            s = 1.0
        out.append(Frame(t_ms=t, bgr=img, scale=1.0 / s))
    cap.release()
    return out


def uniform_frames(video_path: str, every_ms: int = 500,
                   preview_long_side: int = 640) -> list[Frame]:
    cap = cv2.VideoCapture(video_path)
    fps = max(cap.get(cv2.CAP_PROP_FPS), 1e-6)
    total_ms = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps * 1000)
    cap.release()
    return sample_frames(video_path, [(0, total_ms)],
                         per_window=max(1, total_ms // every_ms),
                         preview_long_side=preview_long_side)
