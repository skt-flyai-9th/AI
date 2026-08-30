from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ENGINE_ROOT = Path(__file__).resolve().parents[1] / "reals-video-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from reals_edit_engine.contracts import (  # noqa: E402
    CropMode,
    EditRecipe,
    EffectApplication,
    FinalAudioPolicy,
    QcStatus,
    RecipeSegment,
)
from reals_edit_engine.ffmpeg_graph import _segment_filters, build_render_plan  # noqa: E402
from reals_edit_engine.engine import _validate_segment_duration  # noqa: E402
from reals_edit_engine.media import MediaError  # noqa: E402
from reals_edit_engine.qc import _freeze_windows, post_render_qc  # noqa: E402


def _zoom_segment() -> RecipeSegment:
    return RecipeSegment(
        recipe_segment_id="zoom",
        produced_segment_id="produced",
        sequence_index=1,
        trim_in_ms=0,
        trim_out_ms=400,
        crop_mode=CropMode.KEEP,
        effects=[
            EffectApplication(
                effect_id="ZOOM",
                params={
                    "start_ms": 0,
                    "end_ms": 400,
                    "scale_start": 1.0,
                    "scale_end": 1.03,
                },
            )
        ],
    )


def test_zoom_normalizes_mp4_timebase_before_zoompan(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are required")

    source = tmp_path / "source.mp4"
    output = tmp_path / "zoom.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=180x320:rate=30:duration=0.4",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-video_track_timescale",
            "15360",
            str(source),
        ],
        check=True,
        timeout=30,
    )

    rp = {"width": 180, "height": 320, "fps": 30, "pix_fmt": "yuv420p"}
    filters = _segment_filters(_zoom_segment(), rp)
    assert filters[:3] == ["fps=30", "settb=expr=1/30", "setpts=N"]

    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            ",".join(filters),
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        check=True,
        timeout=30,
    )
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_frames,time_base:format=duration",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    info = json.loads(probe.stdout)
    assert info["streams"][0]["nb_frames"] == "12"
    assert float(info["format"]["duration"]) == pytest.approx(0.4, abs=0.04)


def test_freeze_parser_closes_a_trailing_freeze_at_eof():
    stderr = "[freezedetect] lavfi.freezedetect.freeze_start: 3.566667\n"
    assert _freeze_windows(stderr, 9.733) == [(3.566667, 9.733)]


def test_render_plan_caps_each_filtered_segment_to_its_output_duration(tmp_path):
    segment = _zoom_segment().model_copy(update={"speed_multiplier": 2.0})
    recipe = EditRecipe(
        recipe_id="duration-cap",
        produced_video_id="produced",
        segments=[segment],
    )
    rp = {
        "width": 180,
        "height": 320,
        "fps": 30,
        "pix_fmt": "yuv420p",
        "video_codec": "libx264",
        "crf": 20,
        "preset": "veryfast",
        "x264_profile": "high",
        "level": "4.0",
        "gop": 60,
        "audio_codec": "aac",
        "audio_bitrate": "128k",
        "audio_sample_rate": 48000,
        "movflags": "+faststart",
    }
    commands, _ = build_render_plan(
        recipe,
        "input.mp4",
        None,
        [],
        str(tmp_path / "output.mp4"),
        rp,
        {"sample_rate": 48000},
        200,
        fonts_dir="fonts",
        workdir=str(tmp_path),
        key="duration-cap",
    )
    segment_command = commands[0]
    trim_values = [
        segment_command[index + 1]
        for index, value in enumerate(segment_command[:-1])
        if value == "-t"
    ]
    assert trim_values == ["0.400000", "0.200000"]


def test_post_render_qc_rejects_a_long_trailing_freeze(monkeypatch):
    from reals_edit_engine import qc

    monkeypatch.setattr(
        qc,
        "probe",
        lambda _: {
            "vcodec": "h264",
            "width": 1080,
            "height": 1920,
            "fps": 30.0,
            "pix_fmt": "yuv420p",
            "duration_ms": 9733,
            "size_bytes": 1_000_000,
            "has_audio": True,
            "audio_sample_rate": 48000,
            "audio_codec": "aac",
        },
    )
    outputs = iter(
        [
            "",
            "lavfi.freezedetect.freeze_start: 3.566667",
            "mean_volume: -inf dB\nmax_volume: -inf dB",
        ]
    )
    monkeypatch.setattr(qc, "_ffmpeg_stderr", lambda _: next(outputs))
    report = post_render_qc(
        "output.mp4",
        9733,
        {
            "width": 1080,
            "height": 1920,
            "fps": 30,
            "pix_fmt": "yuv420p",
            "max_duration_sec": 60,
            "max_file_size_bytes": 100_000_000,
            "audio_sample_rate": 48000,
        },
        FinalAudioPolicy.SILENT,
        [],
    )
    frozen = next(check for check in report.checks if check.check_id == "frozen_video")
    assert frozen.status == QcStatus.FAIL
    assert report.status == QcStatus.FAIL


def test_intermediate_segment_duration_rejects_zoom_inflation(monkeypatch):
    from reals_edit_engine import engine

    monkeypatch.setattr(engine, "probe", lambda _: {"duration_ms": 1_484_800})
    with pytest.raises(MediaError, match="중간 컷 길이 불일치"):
        _validate_segment_duration("zoom.mp4", expected_ms=2900, fps=30)
