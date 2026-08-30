"""Post-render QC — exit code 0이어도 통과 못 하면 전달 금지 (구현 문서 22)."""
from __future__ import annotations
import re, subprocess

from .contracts import FinalAudioPolicy, QcCheck, QcReport, QcStatus
from .media import FFMPEG, probe

DUR_TOL_MS = 150          # 프레임 허용 오차
BLACK_MIN_S = 0.35
FREEZE_DETECT_MIN_S = 1.0
FREEZE_MAX_RATIO = 0.25
FREEZE_MIN_LIMIT_S = 2.0
SILENCE_DB = -55.0
SFX_WINDOW_PAD_MS = 300


def _ffmpeg_stderr(args: list[str]) -> str:
    proc = subprocess.run([FFMPEG, "-hide_banner", *args], capture_output=True, text=True)
    return proc.stderr


def _freeze_windows(stderr: str, duration_s: float) -> list[tuple[float, float]]:
    """Parse freezedetect output, including a freeze that continues to EOF."""
    starts = [float(value) for value in re.findall(r"freeze_start:\s*([\d.]+)", stderr)]
    ends = [float(value) for value in re.findall(r"freeze_end:\s*([\d.]+)", stderr)]
    windows = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) and ends[index] >= start else duration_s
        windows.append((start, min(end, duration_s)))
    return windows


def freeze_ratio(path: str, duration_s: float, start_s: float = 0.0) -> float:
    """Return the longest frozen span as a fraction of the inspected window."""
    if duration_s <= 0:
        return 0.0
    args = []
    if start_s > 0:
        args += ["-ss", f"{start_s:.6f}"]
    detect_s = min(FREEZE_DETECT_MIN_S, max(0.15, duration_s * 0.2))
    args += [
        "-t", f"{duration_s:.6f}", "-i", path, "-vf",
        f"freezedetect=n=-50dB:d={detect_s:.6f}",
        "-an", "-f", "null", "-",
    ]
    windows = _freeze_windows(_ffmpeg_stderr(args), duration_s)
    longest_s = max((end - start for start, end in windows), default=0.0)
    return min(1.0, longest_s / duration_s)


def post_render_qc(out_path: str, expected_ms: int, rp: dict,
                   final_audio: FinalAudioPolicy,
                   sfx_windows_ms: list[tuple[int, int]]) -> QcReport:
    checks: list[QcCheck] = []

    def add(cid, ok, detail, warn=False):
        st = QcStatus.PASS if ok else (QcStatus.WARN if warn else QcStatus.FAIL)
        checks.append(QcCheck(check_id=cid, status=st, detail=detail))

    # 22.1 파일·영상
    try:
        info = probe(out_path)
    except Exception as e:
        checks.append(QcCheck(check_id="probe", status=QcStatus.FAIL, detail=str(e)))
        return QcReport.summarize(checks)

    add("codec", info["vcodec"] == "h264", f"vcodec={info['vcodec']}")
    add("resolution", (info["width"], info["height"]) == (rp["width"], rp["height"]),
        f"{info['width']}x{info['height']}")
    add("fps", abs(info["fps"] - rp["fps"]) < 0.1, f"fps={info['fps']}")
    add("pix_fmt", info["pix_fmt"] == rp["pix_fmt"], info["pix_fmt"])
    d = info["duration_ms"] - expected_ms
    add("duration", abs(d) <= DUR_TOL_MS, f"actual={info['duration_ms']}ms "
        f"expected={expected_ms}ms diff={d}ms")
    add("max_duration", info["duration_ms"] / 1000 <= rp["max_duration_sec"],
        f"{info['duration_ms']/1000:.1f}s <= {rp['max_duration_sec']}s")
    add("file_size", info["size_bytes"] <= rp["max_file_size_bytes"],
        f"{info['size_bytes']/1e6:.1f}MB")

    # 검은 프레임
    err = _ffmpeg_stderr(["-i", out_path, "-vf",
                          f"blackdetect=d={BLACK_MIN_S}:pix_th=0.10", "-an", "-f", "null", "-"])
    blacks = re.findall(r"black_start:([\d.]+)", err)
    add("black_frames", not blacks, f"검은 구간 {len(blacks)}개: {blacks[:3]}")

    # A valid container can still contain one duplicated frame for most of the
    # timeline.  Treat a long freeze as render corruption, including a freeze
    # that begins mid-video and continues through EOF.
    duration_s = info["duration_ms"] / 1000
    err = _ffmpeg_stderr([
        "-i", out_path, "-vf",
        f"freezedetect=n=-50dB:d={FREEZE_DETECT_MIN_S}",
        "-an", "-f", "null", "-",
    ])
    freeze_limit_s = max(FREEZE_MIN_LIMIT_S, duration_s * FREEZE_MAX_RATIO)
    freezes = _freeze_windows(err, duration_s)
    long_freezes = [
        (round(start, 3), round(end, 3))
        for start, end in freezes
        if end - start >= freeze_limit_s
    ]
    add(
        "frozen_video",
        not long_freezes,
        f"장시간 정지 구간: {long_freezes[:3]} (limit={freeze_limit_s:.2f}s)"
        if long_freezes else f"장시간 정지 없음 (limit={freeze_limit_s:.2f}s)",
    )

    # 22.4 오디오
    add("audio_stream", info["has_audio"] and info["audio_sample_rate"] == rp["audio_sample_rate"],
        f"audio={info['audio_codec']}@{info['audio_sample_rate']}")

    err = _ffmpeg_stderr(["-i", out_path, "-af", "volumedetect", "-vn", "-f", "null", "-"])
    mv = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB", err)
    mx = re.search(r"max_volume:\s*(-?[\d.]+|-inf)\s*dB", err)
    def _db(m):
        if not m: return 0.0
        return -120.0 if "inf" in m.group(1) else float(m.group(1))
    mean_db = _db(mv)
    max_db = _db(mx)

    if final_audio == FinalAudioPolicy.SILENT:
        add("silent_policy", mean_db <= -70.0, f"mean={mean_db}dB (무음 요구)")
    else:
        add("true_peak", max_db <= -0.9, f"max={max_db}dB (limit -1dBTP)")
        # 비무음 구간이 SFX 창 밖에 있으면 원음 잔존/미승인 오디오
        err = _ffmpeg_stderr(["-i", out_path, "-af",
                              f"silencedetect=n={SILENCE_DB}dB:d=0.25", "-vn", "-f", "null", "-"])
        s_starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", err)]
        s_ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", err)]
        total = info["duration_ms"] / 1000
        nonsilent: list[tuple[float, float]] = []
        cur = 0.0
        for st, en in zip(s_starts, s_ends + [total]):
            if st - cur > 0.05:
                nonsilent.append((cur, st))
            cur = en
        if s_starts and len(s_ends) < len(s_starts):
            pass  # 파일 끝까지 무음
        elif cur < total - 0.05:
            nonsilent.append((cur, total))
        if not s_starts and not s_ends:
            nonsilent = [(0.0, total)] if mean_db > -70 else []
        stray = []
        for a, b in nonsilent:
            covered = any(a * 1000 >= w0 - SFX_WINDOW_PAD_MS and
                          b * 1000 <= w1 + SFX_WINDOW_PAD_MS
                          for w0, w1 in sfx_windows_ms)
            if not covered:
                stray.append((round(a, 2), round(b, 2)))
        add("audio_only_sfx", not stray,
            f"SFX 창 밖 비무음 구간: {stray[:4]}" if stray else
            f"비무음 {len(nonsilent)}곳 모두 SFX 창 안")
    return QcReport.summarize(checks)


def intermediate_qc(out_path: str, expected_ms: int, rp: dict) -> QcReport:
    checks: list[QcCheck] = []
    try:
        info = probe(out_path)
    except Exception as e:
        return QcReport.summarize([QcCheck(check_id="probe", status=QcStatus.FAIL, detail=str(e))])
    ok_d = abs(info["duration_ms"] - expected_ms) <= DUR_TOL_MS
    checks.append(QcCheck(check_id="asm_duration",
                          status=QcStatus.PASS if ok_d else QcStatus.FAIL,
                          detail=f"actual={info['duration_ms']} expected={expected_ms}"))
    ok_r = (info["width"], info["height"]) == (rp["width"], rp["height"])
    checks.append(QcCheck(check_id="asm_resolution",
                          status=QcStatus.PASS if ok_r else QcStatus.FAIL,
                          detail=f"{info['width']}x{info['height']}"))
    return QcReport.summarize(checks)
