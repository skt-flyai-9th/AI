"""OpenCV 품질·모션 분석 — CUT_ASSEMBLY 트림 판정의 실제 근거 (구현 문서 12.2).

모델이 아니라 결정론적 신호 처리. 항상 사용 가능하며 VLM/Pegasus가 없어도
'명백한 앞뒤 대기 구간'은 이걸로 판정한다.
"""
from __future__ import annotations
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class MotionProfile:
    fps: float
    duration_ms: int
    t_ms: list[int]
    motion: list[float]        # 프레임 간 변화량 (0~1 정규화)
    sharpness: list[float]     # Laplacian 분산
    brightness: list[float]    # 0~1
    shake: float               # 전역 흔들림 지표 (낮을수록 안정)

    @property
    def mean_sharpness(self) -> float:
        return float(np.mean(self.sharpness)) if self.sharpness else 0.0

    @property
    def mean_brightness(self) -> float:
        return float(np.mean(self.brightness)) if self.brightness else 0.0


def analyze_motion(video_path: str, step_ms: int = 100,
                   long_side: int = 320) -> MotionProfile:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"영상 열기 실패: {video_path}")
    fps = max(cap.get(cv2.CAP_PROP_FPS), 1e-6)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    dur_ms = int(n / fps * 1000)
    step = max(1, int(fps * step_ms / 1000))

    ts, motion, sharp, bright, centroids = [], [], [], [], []
    prev = None
    i = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                h, w = frame.shape[:2]
                s = long_side / max(h, w)
                small = cv2.resize(frame, (max(2, int(w * s)), max(2, int(h * s))),
                                   interpolation=cv2.INTER_AREA)
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                ts.append(int(i / fps * 1000))
                sharp.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
                bright.append(float(gray.mean() / 255.0))
                if prev is not None:
                    d = cv2.absdiff(gray, prev)
                    motion.append(float((d > 18).mean()))
                    m = cv2.moments((d > 18).astype(np.uint8), binaryImage=True)
                    if m["m00"] > 0:
                        centroids.append((m["m10"] / m["m00"], m["m01"] / m["m00"]))
                else:
                    motion.append(0.0)
                prev = gray
        i += 1
    cap.release()

    shake = 0.0
    if len(centroids) > 2:
        arr = np.asarray(centroids)
        shake = float(np.mean(np.linalg.norm(np.diff(arr, axis=0), axis=1)) / long_side)
    return MotionProfile(fps=fps, duration_ms=dur_ms, t_ms=ts, motion=motion,
                         sharpness=sharp, brightness=bright, shake=shake)


def dead_edges_ms(mp_: MotionProfile, quiet_ratio: float = 0.35,
                  max_edge_ms: int = 1500) -> tuple[int, int]:
    """앞·뒤의 '명백한 정지/대기' 길이. 활동 구간 중앙값 대비 상대 판정."""
    if len(mp_.motion) < 4:
        return 0, 0
    m = np.asarray(mp_.motion[1:])          # 첫 샘플은 항상 0
    t = np.asarray(mp_.t_ms[1:])
    active = m[m > np.percentile(m, 60)]
    if active.size == 0:
        return 0, 0
    thr = float(np.median(active)) * quiet_ratio
    lead = 0
    for tv, mv in zip(t, m):
        if mv > thr:
            break
        lead = int(tv)
    tail = 0
    for tv, mv in zip(t[::-1], m[::-1]):
        if mv > thr:
            break
        tail = int(mp_.duration_ms - tv)
    return min(lead, max_edge_ms), min(tail, max_edge_ms)


def quality_confidence(mp_: MotionProfile) -> float:
    """0~1. 흐림·과다흔들림·노출 이상이면 낮아진다."""
    s = float(np.clip(mp_.mean_sharpness / 120.0, 0, 1))
    b = mp_.mean_brightness
    b_score = float(np.clip(1 - abs(b - 0.5) * 2.2, 0, 1))
    k = float(np.clip(1 - mp_.shake * 12, 0, 1))
    return round(0.45 * s + 0.25 * b_score + 0.30 * k, 3)
