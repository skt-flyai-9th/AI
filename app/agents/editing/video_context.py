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


class VideoContextError(RuntimeError):
    pass


class VideoContextBuilder(Protocol):
    def build(self, videos: list[EditingVideoInput]) -> list[VideoContext]: ...


class FFmpegVideoContextBuilder:
    """Build bounded multimodal evidence from videos without sending MP4 to GPT."""

    def __init__(self) -> None:
        settings = get_settings()
        self.ffprobe_path = settings.editing_ffprobe_path
        self.ffmpeg_path = settings.editing_ffmpeg_path
        self.timeout = settings.editing_probe_timeout_seconds
        self.max_keyframes = settings.editing_max_keyframes_per_video
        self.max_source_duration_ms = settings.editing_max_source_duration_seconds * 1000

    def build(self, videos: list[EditingVideoInput]) -> list[VideoContext]:
        return [self._build_one(video) for video in sorted(videos, key=lambda item: item.shooting_scene_order)]

    def _build_one(self, video: EditingVideoInput) -> VideoContext:
        self._validate_url(video.footage_url)
        metadata = self._probe(video.footage_url, video.video_id)
        duration_ms = metadata["duration_ms"]
        if duration_ms > self.max_source_duration_ms:
            raise VideoContextError(
                f"Video duration exceeds the {self.max_source_duration_ms}ms limit "
                f"for video_id={video.video_id}."
            )
        timestamps = _sample_timestamps(duration_ms, self.max_keyframes)
        keyframes = self._extract_keyframes(video.footage_url, video.video_id, timestamps)
        if not keyframes:
            raise VideoContextError(f"No keyframes could be extracted for video_id={video.video_id}.")
        return VideoContext(
            video_id=video.video_id,
            shooting_scene_order=video.shooting_scene_order,
            duration_ms=duration_ms,
            width=metadata["width"],
            height=metadata["height"],
            fps=metadata["fps"],
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
        if duration_ms < 300 or width <= 0 or height <= 0:
            raise VideoContextError(f"Video metadata was unusable for video_id={video_id}.")
        return {"duration_ms": duration_ms, "width": width, "height": height, "fps": fps}

    def _extract_keyframes(
        self,
        url: str,
        video_id: str,
        timestamps: list[int],
    ) -> list[VideoKeyframe]:
        frames: list[VideoKeyframe] = []
        with tempfile.TemporaryDirectory(prefix="editing-context-") as temp_dir:
            for index, timestamp_ms in enumerate(timestamps):
                output_path = Path(temp_dir) / f"frame-{index}.jpg"
                command = [
                    self.ffmpeg_path,
                    "-v",
                    "error",
                    "-ss",
                    f"{timestamp_ms / 1000:.3f}",
                    "-i",
                    url,
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=720:-2:force_original_aspect_ratio=decrease",
                    "-q:v",
                    "4",
                    "-y",
                    str(output_path),
                ]
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        timeout=self.timeout,
                        check=False,
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise VideoContextError(
                        f"Keyframe extraction failed for video_id={video_id}."
                    ) from exc
                if completed.returncode != 0 or not output_path.exists():
                    continue
                encoded = base64.b64encode(output_path.read_bytes()).decode("ascii")
                frames.append(
                    VideoKeyframe(
                        timestamp_ms=timestamp_ms,
                        image_url=f"data:image/jpeg;base64,{encoded}",
                    )
                )
        return frames


def _sample_timestamps(duration_ms: int, count: int) -> list[int]:
    if count <= 1:
        return [min(duration_ms - 1, duration_ms // 2)]
    last = max(0, duration_ms - 100)
    return sorted({int(round(last * index / (count - 1))) for index in range(count)})


def _parse_frame_rate(value: str) -> float:
    numerator, _, denominator = value.partition("/")
    den = float(denominator or 1)
    return round(float(numerator or 0) / den, 3) if den else 0.0
