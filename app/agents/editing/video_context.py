from __future__ import annotations

import base64
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

from app.agents.editing.types import VideoContext, VideoKeyframe
from app.core.config import get_settings
from app.schemas.editing import EditingVideoInput
from app.services.source_assets import SourceAssetDownloadError, download_source_asset


class VideoContextError(RuntimeError):
    pass


class VideoContextBuilder(Protocol):
    def build(self, videos: list[EditingVideoInput]) -> list[VideoContext]: ...


class FFmpegVideoContextBuilder:
    """Extract frame-accurate evidence once, then let the VLM choose its stride.

    MULTI_CUT analysis consumes every frame before source trimming. ONE_TAKE first
    consumes every third frame for global context, then consumes every frame for
    the final editing decision. Extracting the source only once keeps that quality
    policy feasible on the CPU-only AI host.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.ffprobe_path = settings.editing_ffprobe_path
        self.ffmpeg_path = settings.editing_ffmpeg_path
        self.timeout = settings.editing_probe_timeout_seconds
        self.max_source_duration_ms = settings.editing_max_source_duration_seconds * 1000
        self.analysis_frame_width = int(getattr(settings, "editing_analysis_frame_width", 360))
        self.analysis_jpeg_quality = int(getattr(settings, "editing_analysis_jpeg_quality", 7))
        self.max_download_bytes = settings.renderer_max_download_bytes
        self.download_timeout = settings.renderer_download_timeout_seconds

    def build(self, videos: list[EditingVideoInput]) -> list[VideoContext]:
        return [
            self._build_one(video)
            for video in sorted(videos, key=lambda item: item.shooting_scene_order)
        ]

    def _build_one(self, video: EditingVideoInput) -> VideoContext:
        self._validate_url(video.footage_url)
        with tempfile.TemporaryDirectory(prefix="editing-source-") as temp_dir:
            source_path = Path(temp_dir) / "source.mp4"
            try:
                download_source_asset(
                    video.footage_url,
                    source_path,
                    max_bytes=self.max_download_bytes,
                    timeout_seconds=self.download_timeout,
                )
            except SourceAssetDownloadError as exc:
                raise VideoContextError(
                    f"Video download failed for video_id={video.video_id}."
                ) from exc
            metadata = self._probe(str(source_path), video.video_id)
            duration_ms = int(metadata["duration_ms"])
            if duration_ms > self.max_source_duration_ms:
                raise VideoContextError(
                    f"Video duration exceeds the {self.max_source_duration_ms}ms limit "
                    f"for video_id={video.video_id}."
                )
            timestamps = self._probe_frame_timestamps(
                str(source_path),
                video.video_id,
                fps=float(metadata["fps"]),
                duration_ms=duration_ms,
            )
            keyframes = self._extract_keyframes(str(source_path), video.video_id, timestamps)
        if not keyframes:
            raise VideoContextError(f"No frames could be extracted for video_id={video.video_id}.")
        return VideoContext(
            video_id=video.video_id,
            shooting_scene_order=video.shooting_scene_order,
            duration_ms=duration_ms,
            width=int(metadata["width"]),
            height=int(metadata["height"]),
            fps=float(metadata["fps"]),
            keyframes=keyframes,
        )

    @staticmethod
    def _validate_url(value: str) -> None:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise VideoContextError("footage_url must be an HTTP(S) URL accessible to ffmpeg.")

    def _probe(self, url: str, video_id: str) -> dict[str, int | float]:
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VideoContextError(f"Video probe failed for video_id={video_id}.") from exc
        if completed.returncode != 0:
            raise VideoContextError(f"Video probe failed for video_id={video_id}.")
        try:
            payload = json.loads(completed.stdout)
            stream = payload["streams"][0]
            duration_ms = int(round(float(payload["format"]["duration"]) * 1000))
            width = int(stream["width"])
            height = int(stream["height"])
            fps = _parse_frame_rate(stream.get("avg_frame_rate", "0/1"))
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise VideoContextError(f"Video metadata was invalid for video_id={video_id}.") from exc
        if duration_ms < 300 or width <= 0 or height <= 0 or fps <= 0:
            raise VideoContextError(f"Video metadata was unusable for video_id={video_id}.")
        return {"duration_ms": duration_ms, "width": width, "height": height, "fps": fps}

    def _probe_frame_timestamps(
        self,
        url: str,
        video_id: str,
        *,
        fps: float,
        duration_ms: int,
    ) -> list[int]:
        command = [
            self.ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            url,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(self.timeout, 90),
                check=False,
            )
            if completed.returncode == 0:
                payload = json.loads(completed.stdout)
                result = []
                for frame in payload.get("frames", []):
                    value = frame.get("best_effort_timestamp_time")
                    if value is None:
                        continue
                    result.append(max(0, int(round(float(value) * 1000))))
                if result:
                    return result
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, json.JSONDecodeError):
            pass

        # Constant/average-FPS fallback. The frame index is still exact even if a
        # variable-frame-rate source has a slightly approximate millisecond value.
        count = max(1, int(round(duration_ms * fps / 1000)))
        return [min(duration_ms - 1, int(round(index * 1000 / fps))) for index in range(count)]

    def _extract_keyframes(
        self,
        url: str,
        video_id: str,
        timestamps: list[int],
    ) -> list[VideoKeyframe]:
        """Decode the source once and retain a compact JPEG for every frame."""
        frames: list[VideoKeyframe] = []
        with tempfile.TemporaryDirectory(prefix="editing-context-") as temp_dir:
            pattern = Path(temp_dir) / "frame-%06d.jpg"
            command = [
                self.ffmpeg_path,
                "-v",
                "error",
                "-i",
                url,
                "-map",
                "0:v:0",
                "-vf",
                (
                    f"scale={self.analysis_frame_width}:-2:"
                    "force_original_aspect_ratio=decrease"
                ),
                "-fps_mode",
                "passthrough",
                "-q:v",
                str(self.analysis_jpeg_quality),
                "-y",
                str(pattern),
            ]
            completed = self._run_ffmpeg(command, video_id)
            primary_error = _stderr_tail(completed.stderr)
            if completed.returncode != 0:
                for partial in Path(temp_dir).glob("frame-*.jpg"):
                    partial.unlink(missing_ok=True)

                normalized = Path(temp_dir) / "normalized.mp4"
                normalize = [
                    self.ffmpeg_path,
                    "-v",
                    "error",
                    "-fflags",
                    "+genpts",
                    "-err_detect",
                    "ignore_err",
                    "-i",
                    url,
                    "-map",
                    "0:v:0",
                    "-vf",
                    (
                        f"scale={self.analysis_frame_width}:-2:"
                        "force_original_aspect_ratio=decrease,"
                        "setpts=PTS-STARTPTS,format=yuv420p"
                    ),
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-crf",
                    "23",
                    "-movflags",
                    "+faststart",
                    "-y",
                    str(normalized),
                ]
                normalized_result = self._run_ffmpeg(normalize, video_id)
                if normalized_result.returncode == 0:
                    retry = [
                        self.ffmpeg_path,
                        "-v",
                        "error",
                        "-i",
                        str(normalized),
                        "-map",
                        "0:v:0",
                        "-fps_mode",
                        "passthrough",
                        "-q:v",
                        str(self.analysis_jpeg_quality),
                        "-y",
                        str(pattern),
                    ]
                    completed = self._run_ffmpeg(retry, video_id)
                else:
                    completed = normalized_result

            if completed.returncode != 0:
                retry_error = _stderr_tail(completed.stderr)
                details = " | ".join(
                    detail for detail in (primary_error, retry_error) if detail
                )
                suffix = f" ffmpeg: {details}" if details else ""
                raise VideoContextError(
                    f"Frame extraction failed for video_id={video_id}.{suffix}"
                )

            paths = sorted(Path(temp_dir).glob("frame-*.jpg"))
            if not paths:
                return []
            if len(timestamps) < len(paths):
                last = timestamps[-1] if timestamps else 0
                timestamps = timestamps + [last] * (len(paths) - len(timestamps))
            for frame_index, output_path in enumerate(paths):
                timestamp_ms = timestamps[min(frame_index, len(timestamps) - 1)]
                encoded = base64.b64encode(output_path.read_bytes()).decode("ascii")
                frames.append(
                    VideoKeyframe(
                        frame_index=frame_index,
                        timestamp_ms=timestamp_ms,
                        image_url=f"data:image/jpeg;base64,{encoded}",
                    )
                )
        return frames

    def _run_ffmpeg(self, command: list[str], video_id: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                timeout=max(self.timeout, 180),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VideoContextError(
                f"Frame extraction failed for video_id={video_id}: {type(exc).__name__}."
            ) from exc


def _sample_timestamps(duration_ms: int, count: int) -> list[int]:
    """Backward-compatible helper retained for tests and utility callers."""
    if count <= 1:
        return [min(duration_ms - 1, duration_ms // 2)]
    last = max(0, duration_ms - 100)
    return sorted({int(round(last * index / (count - 1))) for index in range(count)})


def _stderr_tail(value: bytes | str | None, *, limit: int = 2000) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return " ".join((value or "").strip().split())[-limit:]


def _parse_frame_rate(value: str) -> float:
    numerator, _, denominator = value.partition("/")
    den = float(denominator or 1)
    return round(float(numerator or 0) / den, 3) if den else 0.0
