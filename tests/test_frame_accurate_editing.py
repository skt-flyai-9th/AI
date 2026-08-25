from __future__ import annotations

import sys
from pathlib import Path

from app.agents.editing.llm import (
    OpenAIEditingLLM,
    _apply_source_preparation,
    _map_cut_analysis_to_produced,
    _normalize_source_cut_plan,
    _resolve_shoot_mode,
)
from app.agents.editing.reals import RealsRegistry
from app.agents.editing.types import (
    EditingPlanDecision,
    FrameObservation,
    SourceCutDecision,
    SourceCutPlan,
    VideoContext,
    VideoKeyframe,
)
from app.schemas.editing import EditRecipe, PublishingResult
from app.schemas.template_knowledge import VideoEditingDBRules


def _context(video_id: str, order: int, count: int = 10, fps: float = 30.0) -> VideoContext:
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
        shooting_scene_order=order,
        duration_ms=max(1000, frames[-1].timestamp_ms + int(round(1000 / fps))),
        width=1080,
        height=1920,
        fps=fps,
        keyframes=frames,
    )


class _ProbePlanner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[int]]] = []

    def _analyze_video_frames(self, *, context, frames, purpose, **kwargs):
        self.calls.append((purpose, [item.frame_index for item in frames]))
        return {
            "video_id": context.video_id,
            "shooting_scene_order": context.shooting_scene_order,
            "duration_ms": context.duration_ms,
            "fps": context.fps,
            "summary": purpose,
            "observations": [
                FrameObservation(
                    video_id=context.video_id,
                    frame_index=item.frame_index,
                    timestamp_ms=item.timestamp_ms,
                    semantic_event="ACTION" if item.frame_index else "HOOK",
                    cut_transition_candidate=item.frame_index in {1, len(context.keyframes) - 2},
                    cut_transition_score=0.9 if item.frame_index in {1, len(context.keyframes) - 2} else 0.1,
                ).model_dump(mode="json")
                for item in frames
            ],
        }

    def _plan_source_cuts(self, *, video_contexts, **kwargs):
        return SourceCutPlan(
            cuts=[
                SourceCutDecision(
                    video_id=context.video_id,
                    trim_in_ms=context.keyframes[1].timestamp_ms,
                    trim_out_ms=context.keyframes[-2].timestamp_ms,
                    mapped_reference_segment_id=f"ref_{context.shooting_scene_order}",
                    cut_in_reason="action start",
                    cut_out_reason="transition candidate",
                    decision_reason="match reference segment",
                )
                for context in video_contexts
            ],
            rationale="reference-aligned cuts",
        )


def test_one_take_reads_stride_three_then_every_frame():
    planner = _ProbePlanner()
    context = _context("take_1", 1, count=10)

    result = OpenAIEditingLLM._prepare_frame_analysis(
        planner,
        video_contexts=[context],
        video_editing_db={},
        reference_context={},
        revision_action=None,
        shoot_mode="ONE_TAKE",
    )

    assert planner.calls == [
        ("ONE_TAKE_GLOBAL_EVERY_3_FRAMES", [0, 3, 6, 9]),
        ("ONE_TAKE_FINAL_FRAME_EXACT", list(range(10))),
    ]
    assert result["source_preparation"]["mode"] == "ONE_TAKE_PASSTHROUGH"
    assert len(result["produced_frame_context"]["observations"]) == 10


def test_multi_cut_reads_each_raw_cut_every_frame_without_reread():
    planner = _ProbePlanner()
    first = _context("cut_1", 1, count=8)
    second = _context("cut_2", 2, count=7)

    result = OpenAIEditingLLM._prepare_frame_analysis(
        planner,
        video_contexts=[first, second],
        video_editing_db={},
        reference_context={"video_insights": [{"shot_sequence": ["hook", "result"]}]},
        revision_action=None,
        shoot_mode="MULTI_CUT",
    )

    assert planner.calls == [
        ("MULTI_CUT_SOURCE_FRAME_EXACT", list(range(8))),
        ("MULTI_CUT_SOURCE_FRAME_EXACT", list(range(7))),
    ]
    assert result["source_preparation"]["mode"] == "MULTI_CUT"
    assert result["produced_frame_context"]["mode"] == "MULTI_CUT"


def test_cut_plan_is_snapped_to_real_frames_and_capture_order():
    first = _context("cut_1", 1, count=6)
    second = _context("cut_2", 2, count=6)
    analyzed = []
    for context in [first, second]:
        analyzed.append(
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
        )

    plan = SourceCutPlan(
        cuts=[
            SourceCutDecision(
                video_id="cut_2",
                trim_in_ms=40,
                trim_out_ms=140,
                mapped_reference_segment_id="ref_2",
                cut_in_reason="start",
                cut_out_reason="end",
                decision_reason="test",
            ),
            SourceCutDecision(
                video_id="cut_1",
                trim_in_ms=40,
                trim_out_ms=140,
                mapped_reference_segment_id="ref_1",
                cut_in_reason="start",
                cut_out_reason="end",
                decision_reason="test",
            ),
        ],
        rationale="test",
    )

    normalized = _normalize_source_cut_plan(plan, [first, second], analyzed)

    assert [item.video_id for item in normalized.cuts] == ["cut_1", "cut_2"]
    valid_first = {item.timestamp_ms for item in first.keyframes}
    assert normalized.cuts[0].trim_in_ms in valid_first
    assert normalized.cuts[0].trim_out_ms in valid_first


def test_multi_cut_analysis_is_reused_on_produced_timeline():
    analyzed = [
        {
            "video_id": "cut_1",
            "observations": [
                FrameObservation(video_id="cut_1", frame_index=0, timestamp_ms=0).model_dump(mode="json"),
                FrameObservation(video_id="cut_1", frame_index=1, timestamp_ms=100).model_dump(mode="json"),
                FrameObservation(video_id="cut_1", frame_index=2, timestamp_ms=200).model_dump(mode="json"),
            ],
        },
        {
            "video_id": "cut_2",
            "observations": [
                FrameObservation(video_id="cut_2", frame_index=0, timestamp_ms=0).model_dump(mode="json"),
                FrameObservation(video_id="cut_2", frame_index=1, timestamp_ms=100).model_dump(mode="json"),
                FrameObservation(video_id="cut_2", frame_index=2, timestamp_ms=200).model_dump(mode="json"),
            ],
        },
    ]
    plan = SourceCutPlan(
        cuts=[
            SourceCutDecision(
                video_id="cut_1",
                trim_in_ms=100,
                trim_out_ms=200,
                mapped_reference_segment_id="ref_1",
                cut_in_reason="start",
                cut_out_reason="end",
                decision_reason="test",
            ),
            SourceCutDecision(
                video_id="cut_2",
                trim_in_ms=0,
                trim_out_ms=200,
                mapped_reference_segment_id="ref_2",
                cut_in_reason="start",
                cut_out_reason="end",
                decision_reason="test",
            ),
        ],
        rationale="test",
    )

    produced = _map_cut_analysis_to_produced(analyzed, plan)

    assert [item["produced_timestamp_ms"] for item in produced["observations"]] == [0, 100, 100, 200, 300]
    assert produced["observations"][0]["mapped_reference_segment_id"] == "ref_1"
    assert produced["observations"][-1]["mapped_reference_segment_id"] == "ref_2"


def test_source_preparation_overrides_llm_cut_boundaries():
    recipe = EditRecipe.model_validate(
        {
            "editing_template_id": "db",
            "editing_template_version": 1,
            "timeline": [
                {
                    "clip_order": 2,
                    "video_id": "cut_2",
                    "source_start_ms": 0,
                    "source_end_ms": 900,
                    "timeline_start_ms": 900,
                    "speed": 1.0,
                },
                {
                    "clip_order": 1,
                    "video_id": "cut_1",
                    "source_start_ms": 0,
                    "source_end_ms": 900,
                    "timeline_start_ms": 0,
                    "speed": 2.0,
                },
            ],
            "cta": {"text": "지금 확인해보세요"},
        }
    )
    decision = EditingPlanDecision(
        outcome="RECIPE",
        recipe=recipe,
        publishing=PublishingResult(caption="테스트", hashtags=[]),
        missing_scene_roles=[],
        available_options=[],
        rationale="test",
    )
    source = {
        "mode": "MULTI_CUT",
        "cuts": [
            {"video_id": "cut_1", "trim_in_ms": 100, "trim_out_ms": 500},
            {"video_id": "cut_2", "trim_in_ms": 200, "trim_out_ms": 800},
        ],
    }

    result = _apply_source_preparation(
        decision,
        source,
        [_context("cut_1", 1), _context("cut_2", 2)],
    )

    assert [(c.video_id, c.source_start_ms, c.source_end_ms) for c in result.recipe.timeline] == [
        ("cut_1", 100, 500),
        ("cut_2", 200, 800),
    ]
    assert result.recipe.timeline[0].timeline_start_ms == 0
    assert result.recipe.timeline[1].timeline_start_ms == 200


def test_one_take_source_preparation_keeps_full_source():
    context = _context("one", 1, count=10)
    recipe = EditRecipe.model_validate(
        {
            "editing_template_id": "db",
            "editing_template_version": 1,
            "timeline": [
                {
                    "clip_order": 1,
                    "video_id": "one",
                    "source_start_ms": 100,
                    "source_end_ms": 500,
                    "timeline_start_ms": 0,
                    "speed": 1.0,
                }
            ],
            "cta": {"text": "확인해보세요"},
        }
    )
    decision = EditingPlanDecision(
        outcome="RECIPE",
        recipe=recipe,
        publishing=PublishingResult(caption="테스트", hashtags=[]),
        missing_scene_roles=[],
        available_options=[],
        rationale="test",
    )

    result = _apply_source_preparation(
        decision,
        {"mode": "ONE_TAKE_PASSTHROUGH"},
        [context],
    )

    assert result.recipe.timeline[0].source_start_ms == 0
    assert result.recipe.timeline[0].source_end_ms == context.duration_ms


def test_shoot_mode_is_backward_compatible():
    one = _context("one", 1)
    two = _context("two", 2)
    assert _resolve_shoot_mode({}, [one]) == "ONE_TAKE"
    assert _resolve_shoot_mode({}, [one, two]) == "MULTI_CUT"
    assert _resolve_shoot_mode({"shoot_mode": "CUT"}, [one]) == "MULTI_CUT"


def test_video_editing_db_columns_are_not_extended_for_reference_effects():
    assert set(VideoEditingDBRules.model_fields) == {
        "source_type",
        "render_profile_id",
        "assembly_profile_id",
        "safe_area_profile_id",
        "audio_policy",
        "min_cut_duration_ms",
        "max_duration_sec",
        "allowed_effect_ids",
        "allowed_transition_ids",
    }


def test_reals_registry_exposes_new_cpu_transform_effects():
    registry = RealsRegistry()
    assert {
        "SHAKE",
        "VIBRATION",
        "ROTATION",
        "ZOOM",
        "POSITION_MOVE",
        "FLASH",
        "COLOR",
    } <= registry.creative_effect_ids


def test_reals_filter_graph_renders_timed_transforms_and_normalizes_vertical():
    engine_root = Path(__file__).resolve().parents[1] / "reals-video-engine"
    if str(engine_root) not in sys.path:
        sys.path.insert(0, str(engine_root))

    from reals_edit_engine.contracts import CropMode, EffectApplication, RecipeSegment
    from reals_edit_engine.ffmpeg_graph import _segment_filters

    segment = RecipeSegment(
        recipe_segment_id="seg",
        produced_segment_id="produced",
        sequence_index=1,
        trim_in_ms=0,
        trim_out_ms=2000,
        crop_mode=CropMode.CENTER_9_16,
        effects=[
            EffectApplication(
                effect_id="SHAKE",
                params={
                    "start_ms": 200,
                    "end_ms": 400,
                    "amplitude_x_pct": 0.018,
                    "amplitude_y_pct": 0.007,
                    "rotation_deg": 0.5,
                    "scale": 1.018,
                    "frequency_hz": 12.0,
                    "damping": True,
                },
            ),
            EffectApplication(
                effect_id="FLASH",
                params={"start_ms": 500, "end_ms": 570, "opacity": 0.8},
            ),
            EffectApplication(
                effect_id="COLOR",
                params={"start_ms": 600, "end_ms": 1200, "tone": "VIVID"},
            ),
        ],
    )
    rp = {"width": 1080, "height": 1920, "fps": 30, "pix_fmt": "yuv420p"}

    filters = _segment_filters(segment, rp)
    joined = ",".join(filters)

    assert "force_original_aspect_ratio=increase" in joined
    assert "crop=1080:1920" in joined
    assert "rotate=" in joined
    assert "drawbox=" in joined
    assert "enable='between(t,0.600000,1.200000)'" in joined
