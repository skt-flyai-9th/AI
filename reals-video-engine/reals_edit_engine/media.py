"""미디어 코어 — probe / normalize / hash / ffmpeg 실행 (구현 문서 10.3, 12.7)."""
from __future__ import annotations
import hashlib, json, pathlib, subprocess

from .contracts import MediaFileRef

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


class MediaError(Exception):
    pass


def sha256_file(path: str | pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    """shell=False 고정 — 사용자 입력이 셸 문자열로 들어가지 않는다 (21.1)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-8:]
        raise MediaError(f"명령 실패({cmd[0]} rc={proc.returncode}):\n" + "\n".join(tail))
    return proc


def probe(path: str | pathlib.Path) -> dict:
    p = pathlib.Path(path)
    if not p.exists() or p.stat().st_size == 0:
        raise MediaError(f"파일 없음/빈 파일: {p}")
    proc = run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(p)])
    data = json.loads(proc.stdout)
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise MediaError(f"비디오 스트림 없음: {p}")
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = (float(num) / float(den)) if float(den) else 0.0
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return {
        "path": str(p),
        "duration_ms": int(round(float(data["format"]["duration"]) * 1000)),
        "width": int(v["width"]), "height": int(v["height"]),
        "fps": round(fps, 3), "pix_fmt": v.get("pix_fmt", ""),
        "vcodec": v["codec_name"], "rotation": rotation,
        "r_frame_rate": v.get("r_frame_rate", ""),
        "has_audio": a is not None,
        "audio_codec": a["codec_name"] if a else "",
        "audio_sample_rate": int(a["sample_rate"]) if a else 0,
        "size_bytes": int(data["format"].get("size", 0)),
    }


def media_ref(file_id: str, path: str | pathlib.Path) -> MediaFileRef:
    info = probe(path)
    return MediaFileRef(
        file_id=file_id, path=str(path), sha256=sha256_file(path),
        duration_ms=info["duration_ms"], width=info["width"],
        height=info["height"], fps=info["fps"],
    )


def normalize(src: str | pathlib.Path, dst: str | pathlib.Path, profile: dict,
              keep_audio: bool = True) -> dict:
    """세로 캔버스 정규화: scale-to-cover → center crop → CFR → yuv420p.

    회전 메타데이터는 ffmpeg autorotate가 픽셀 회전으로 실체화한다.
    이후 모든 타임코드는 이 intermediate 기준으로 계산한다.
    """
    w, h, fps = profile["width"], profile["height"], profile["fps"]
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
          f"crop={w}:{h},setsar=1,fps={fps},format={profile['pix_fmt']}")
    from .ffmpeg_graph import video_encode_args
    cmd = [FFMPEG, "-hide_banner", "-y", "-i", str(src), "-vf", vf,
           *video_encode_args(profile),
           "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if keep_audio:
        cmd += ["-c:a", profile["audio_codec"], "-b:a", profile["audio_bitrate"],
                "-ar", str(profile["audio_sample_rate"]), "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += ["-movflags", profile["movflags"], "-map_metadata", "-1", str(dst)]
    run(cmd)
    return probe(dst)
