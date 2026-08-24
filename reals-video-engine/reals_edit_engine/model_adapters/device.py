"""디바이스·VRAM 예산 관리.

RTX 4090 Laptop(16GB) 기준. 어댑터는 lazy load하고 VRAM 압박 시 release()로 내린다.
CUDA가 없으면 자동으로 CPU로 떨어진다(코드 경로는 동일).
"""
from __future__ import annotations
import functools, os, subprocess


@functools.lru_cache(maxsize=1)
def cuda_available() -> bool:
    if os.environ.get("REALS_FORCE_CPU") == "1":
        return False
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        pass
    try:
        subprocess.run(["nvidia-smi"], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def gpu_info() -> dict:
    if not cuda_available():
        return {"device": "cpu", "name": "cpu", "vram_mb": 0}
    try:
        import torch
        p = torch.cuda.get_device_properties(0)
        return {"device": "cuda", "name": p.name,
                "vram_mb": int(p.total_memory / 1024 / 1024)}
    except Exception:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout.strip()
        name, mem = (out.split(",") + ["?", "0"])[:2]
        return {"device": "cuda", "name": name.strip(), "vram_mb": int(mem)}


def device_str() -> str:
    return "cuda" if cuda_available() else "cpu"


def onnx_providers() -> list:
    """rapidocr/onnxruntime 공통 provider 선택."""
    try:
        import onnxruntime as ort
        try:
            ort.preload_dlls()
        except (AttributeError, OSError):
            pass
        avail = ort.get_available_providers()
    except Exception:
        return ["CPUExecutionProvider"]
    for p in ("CUDAExecutionProvider", "TensorrtExecutionProvider"):
        if p in avail:
            return [p, "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


@functools.lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """NVENC 실탐지 — 0.1초 테스트 인코드.

    CUDA가 있어도 NVENC이 없을 수 있다(WSL2: CUDA 지원, NVENC 미지원).
    인코더 목록만 보지 않고 실제 인코드로 확인한다.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error",
             "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1:r=30",
             "-c:v", "h264_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def available_ram_gb() -> float:
    """Linux(/proc) · Windows(ctypes) · psutil 순 폴백."""
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        pass
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) / 1024 / 1024
    except Exception:
        pass
    try:
        import ctypes
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MS(); st.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullAvailPhys / (1024 ** 3)
    except Exception:
        return 0.0


def free_cuda():
    try:
        import torch, gc
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
