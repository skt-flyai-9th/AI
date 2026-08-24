"""Avoid Map 합성기 — 여러 어댑터 결과를 병합해 AvoidMap을 만든다.

어댑터 실패는 치명적이지 않다(구현 문서 27):
  SAM/Tracking 실패 → 하위 어댑터로 폴백 + WARN
  OCR 실패          → 기본 Safe Area 배치 + WARN
"""
from __future__ import annotations
from dataclasses import dataclass, field

from ..contracts import AvoidMap, AvoidRegion
from ..subtitle_layout import AvoidMapProvider
from .device import cuda_available, free_cuda
from .frames import sample_frames
from .vision import (MediaPipeFaceAdapter, MediaPipeObjectAdapter,
                     MediaPipePoseAdapter, MediaPipeSegmentAdapter, VisionAdapter)


@dataclass
class VisionAvoidMapProvider(AvoidMapProvider):
    """실제 모델 기반 Provider. 기본 스택은 환경에 맞춰 자동 구성."""
    adapters: list[VisionAdapter] = field(default_factory=list)
    frames_per_window: int = 3
    preview_long_side: int = 640
    warnings: list[str] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    verbose: bool = True

    @classmethod
    def default_stack(cls, prefer_gpu: bool = True, with_ocr: bool = True,
                      **kw) -> "VisionAvoidMapProvider":
        adapters: list[VisionAdapter] = [MediaPipeFaceAdapter(), MediaPipePoseAdapter()]
        if prefer_gpu and cuda_available():
            from .vision_gpu import Sam3Adapter, YoloDetectorAdapter
            adapters += [Sam3Adapter(), YoloDetectorAdapter()]
        else:
            adapters += [MediaPipeSegmentAdapter(), MediaPipeObjectAdapter()]
        if with_ocr:
            from .vision import RapidOcrTextAdapter
            adapters.append(RapidOcrTextAdapter())
        return cls(adapters=adapters, **kw)

    def analyze(self, video_path: str, windows_ms: list[tuple[int, int]]) -> AvoidMap:
        self.warnings, self.used = [], []
        if not windows_ms:
            return AvoidMap()
        frames = sample_frames(video_path, windows_ms,
                               per_window=self.frames_per_window,
                               preview_long_side=self.preview_long_side)
        regions: list[AvoidRegion] = []
        for ad in self.adapters:
            if self.verbose:
                print(f"    · {ad.name} ...", flush=True)
            try:
                got = ad.regions(frames)
                regions.extend(got)
                self.used.append(f"{ad.name}({len(got)})")
            except Exception as e:
                self.warnings.append(f"{ad.name} 실패 → 폴백: {type(e).__name__}: {e}")
                for fb in self._fallbacks(ad):
                    try:
                        got = fb.regions(frames)
                        regions.extend(got)
                        self.used.append(f"{fb.name}(fallback,{len(got)})")
                        break
                    except Exception as e2:
                        self.warnings.append(f"{fb.name} 폴백도 실패: {e2}")
                    finally:
                        try:
                            fb.close()
                        except Exception:
                            pass
                        free_cuda()
            finally:
                try:
                    ad.close()
                except Exception:
                    pass
                free_cuda()
        free_cuda()
        return AvoidMap(regions=_merge(regions))

    @staticmethod
    def _fallbacks(ad: VisionAdapter) -> list[VisionAdapter]:
        n = ad.name
        if n.startswith("sam3"):
            return [MediaPipeSegmentAdapter()]
        if n.startswith("ultralytics"):
            return [MediaPipeObjectAdapter()]
        return []


def _iou(a: AvoidRegion, b: AvoidRegion) -> float:
    ix = max(0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
    iy = max(0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y))
    inter = ix * iy
    if inter == 0:
        return 0.0
    return inter / float(a.w * a.h + b.w * b.h - inter)


def _merge(regions: list[AvoidRegion], iou_thr: float = 0.6) -> list[AvoidRegion]:
    """같은 라벨·높은 IoU는 시간창을 합쳐 하나로 — 영역 폭발 방지."""
    regions = sorted(regions, key=lambda r: (-r.priority, r.label, r.start_ms))
    merged: list[AvoidRegion] = []
    for r in regions:
        hit = None
        for m in merged:
            if m.label.split(":")[0] == r.label.split(":")[0] and _iou(m, r) >= iou_thr:
                hit = m
                break
        if hit is None:
            merged.append(r.model_copy())
        else:
            x0, y0 = min(hit.x, r.x), min(hit.y, r.y)
            x1 = max(hit.x + hit.w, r.x + r.w)
            y1 = max(hit.y + hit.h, r.y + r.h)
            hit.x, hit.y, hit.w, hit.h = x0, y0, x1 - x0, y1 - y0
            hit.start_ms = min(hit.start_ms, r.start_ms)
            hit.end_ms = max(hit.end_ms, r.end_ms)
            hit.priority = max(hit.priority, r.priority)
    return merged
