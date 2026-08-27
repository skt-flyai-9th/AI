from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents.editing.llm import EditingLLMError, _normalize_source_cut_plan
from app.agents.editing.types import (
    FrameObservation,
    SourceCutDecision,
    SourceCutPlan,
    VideoContext,
    VideoKeyframe,
)


def _context(video_id: str = "cut", *, count: int = 20, fps: float = 30.0) -> VideoContext:
    frames = [
        VideoKeyframe(
            frame_index=index,
            timestamp_ms=int(round(index * 1000 / fps)),
            image_url=f"data:image/jpeg;base64,{index}",
        )
        for index in range(count)
    ]
    return VideoContext(
        video_id=video_id,
        shooting_scene_order=1,
        duration_ms=1000,
        width=1080,
        height=1920,
        fps=fps,
        keyframes=frames,
    )


def _analyzed(context: VideoContext) -> list[dict]:
    return [
        {
            "video_id": context.video_id,
            "observations": [
                FrameObservation(
                    video_id=context.video_id,
                    frame_index=frame.frame_index,
                    timestamp_ms=frame.timestamp_ms,
                ).model_dump(mode="json")
                for frame in context.keyframes
            ],
        }
    ]


def test_short_frame_exact_cut_is_expanded_before_recipe_repair_loop():
    context = _context(count=20)
    plan = SourceCutPlan(
        cuts=[
            SourceCutDecision(
                video_id=context.video_id,
                trim_in_ms=200,
                trim_out_ms=267,
                mapped_reference_segment_id="ref_1",
                cut_in_reason="action start",
                cut_out_reason="action end",
                decision_reason="reference match",
            )
        ],
        rationale="test",
    )

    normalized = _normalize_source_cut_plan(
        plan,
        [context],
        _analyzed(context),
        min_cut_ms=300,
    )

    cut = normalized.cuts[0]
    timestamps = {frame.timestamp_ms for frame in context.keyframes}
    assert cut.trim_in_ms in timestamps
    assert cut.trim_out_ms in timestamps
    assert cut.trim_out_ms - cut.trim_in_ms >= 300


def test_impossible_minimum_frame_exact_cut_fails_before_repair_loop():
    context = _context(count=5)
    plan = SourceCutPlan(
        cuts=[
            SourceCutDecision(
                video_id=context.video_id,
                trim_in_ms=0,
                trim_out_ms=100,
                mapped_reference_segment_id="ref_1",
                cut_in_reason="start",
                cut_out_reason="end",
                decision_reason="test",
            )
        ],
        rationale="test",
    )

    with pytest.raises(EditingLLMError, match="minimum cut duration"):
        _normalize_source_cut_plan(
            plan,
            [context],
            _analyzed(context),
            min_cut_ms=300,
        )


def test_new_reals_effect_filters_execute_in_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is not installed on this test host")

    engine_root = Path(__file__).resolve().parents[1] / "reals-video-engine"
    if str(engine_root) not in sys.path:
        sys.path.insert(0, str(engine_root))

    from reals_edit_engine.contracts import CropMode, EffectApplication, RecipeSegment
    from reals_edit_engine.ffmpeg_graph import _segment_filters

    cases = [
        (
            "SHAKE",
            {
                "start_ms": 50,
                "end_ms": 250,
                "amplitude_x_pct": 0.012,
                "amplitude_y_pct": 0.006,
                "rotation_deg": 0.5,
                "scale": 1.018,
                "frequency_hz": 12.0,
                "damping": True,
            },
        ),
        (
            "VIBRATION",
            {
                "start_ms": 50,
                "end_ms": 250,
                "amplitude_x_pct": 0.006,
                "amplitude_y_pct": 0.004,
                "rotation_deg": 0.15,
                "scale": 1.012,
                "frequency_hz": 20.0,
                "damping": True,
            },
        ),
        ("ROTATION", {"start_ms": 50, "end_ms": 250, "rotation_deg": 0.8, "scale": 1.02}),
        ("ZOOM", {"start_ms": 50, "end_ms": 250, "scale_start": 1.0, "scale_end": 1.08}),
        (
            "POSITION_MOVE",
            {
                "start_ms": 50,
                "end_ms": 250,
                "translate_x_pct": 0.02,
                "translate_y_pct": -0.01,
                "scale": 1.03,
            },
        ),
        ("FLASH", {"start_ms": 50, "end_ms": 120, "opacity": 0.8}),
        ("COLOR", {"start_ms": 50, "end_ms": 250, "tone": "VIVID"}),
    ]
    rp = {"width": 180, "height": 320, "fps": 30, "pix_fmt": "yuv420p"}

    for effect_id, params in cases:
        segment = RecipeSegment(
            recipe_segment_id=f"seg_{effect_id.lower()}",
            produced_segment_id="produced",
            sequence_index=1,
            trim_in_ms=0,
            trim_out_ms=400,
            crop_mode=CropMode.CENTER_9_16,
            effects=[EffectApplication(effect_id=effect_id, params=params)],
        )
        vf = ",".join(_segment_filters(segment, rp))
        completed = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=180x320:r=30:d=0.4",
                "-vf",
                vf,
                "-frames:v",
                "12",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, f"{effect_id}: {completed.stderr}"
