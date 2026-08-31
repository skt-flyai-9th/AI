"""Regression tests for run-blocking editing-pipeline failures.

Covers the renderer-entry contract gaps where a recipe passes the app-side
validator/repair loop but still dies at (or before) the REALS engine.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.agents.editing import service as service_module
from app.agents.editing.renderer import RendererError
from app.agents.editing.service import EditingAgentService
from app.agents.editing.validator import EditRecipeValidator
from app.schemas.editing import EditingRenderResult, SelectedShortform
from tests.test_editing_agent import (
    FakeRenderer,
    FakeVideoContextBuilder,
    RepairingFakeLLM,
    _recipe,
    _request,
)

ENGINE_ROOT = Path(__file__).resolve().parents[1] / "reals-video-engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from reals_edit_engine.contracts import (  # noqa: E402
    EditRecipe as EngineEditRecipe,
    MediaFileRef,
    MotionId,
    Overlay,
    OverlayType,
    RecipeSegment,
)
from reals_edit_engine.ffmpeg_graph import build_concat_plan  # noqa: E402
from reals_edit_engine.registries import Registries  # noqa: E402
from reals_edit_engine.validator import ValidationError, validate_recipe  # noqa: E402


def _service(renderer=None) -> EditingAgentService:
    return EditingAgentService(
        llm=RepairingFakeLLM(),
        video_context_builder=FakeVideoContextBuilder(),
        renderer=renderer or FakeRenderer(),
    )


def _fallback_video_editing_db(scenes: list[dict]) -> dict:
    return {
        "editing_template_id": "video_editing_db_014",
        "editing_template_version": 3,
        "name": "메뉴 공개",
        "recommendation_title": "한눈에 보는 신메뉴",
        "shooting_guide": {"scenes": scenes},
        "editing_rules": {
            "min_cut_duration_ms": 300,
            "max_duration_sec": 30,
        },
    }


def test_ordered_fallback_clamps_clip_durations_to_template_slots():
    service = _service()
    request = _request()
    contexts = FakeVideoContextBuilder().build(request.videos)

    decision = service._build_ordered_fallback(
        request,
        _fallback_video_editing_db(
            [
                {"scene_order": 1, "target_duration_sec": 1.5},
                {"scene_order": 2, "target_duration_sec": 2.0},
            ]
        ),
        contexts,
    )

    assert decision.outcome == "RECIPE"
    durations = [
        clip.source_end_ms - clip.source_start_ms for clip in decision.recipe.timeline
    ]
    assert durations == [1500, 2000]


def test_ordered_fallback_skips_scene_whose_slot_is_below_min_cut():
    service = _service()
    request = _request()
    contexts = FakeVideoContextBuilder().build(request.videos)

    decision = service._build_ordered_fallback(
        request,
        _fallback_video_editing_db(
            [
                {"scene_order": 1, "target_duration_sec": 0.2},
                {"scene_order": 2, "target_duration_sec": 3.0},
            ]
        ),
        contexts,
    )

    assert [clip.video_id for clip in decision.recipe.timeline] == ["take_502"]
    clip = decision.recipe.timeline[0]
    assert clip.source_end_ms - clip.source_start_ms == 3000


class _FlakyRenderer:
    def __init__(self, error_codes: list[str], *, retryable: bool = True) -> None:
        self.error_codes = list(error_codes)
        self.retryable = retryable
        self.calls = 0

    def render(self, **kwargs):
        self.calls += 1
        if self.error_codes:
            raise RendererError(
                "renderer failed",
                code=self.error_codes.pop(0),
                retryable=self.retryable,
            )
        return EditingRenderResult(
            output_video_url="https://cdn.example/final.mp4",
            resolution="1080x1920",
            duration_sec=4.0,
        )


def test_render_retry_recovers_from_transient_renderer_errors(monkeypatch):
    monkeypatch.setattr(service_module.time, "sleep", lambda seconds: None)
    renderer = _FlakyRenderer(["RENDERER_NETWORK_ERROR", "RENDERER_HTTP_503"])
    service = _service(renderer=renderer)

    result = service._render_with_retry(run_id="run_retry")

    assert result.output_video_url == "https://cdn.example/final.mp4"
    assert renderer.calls == 3


def test_render_retry_surfaces_timeout_and_nonretryable_immediately(monkeypatch):
    monkeypatch.setattr(service_module.time, "sleep", lambda seconds: None)

    timeout_renderer = _FlakyRenderer(["RENDERER_TIMEOUT", "RENDERER_TIMEOUT"])
    with pytest.raises(RendererError):
        _service(renderer=timeout_renderer)._render_with_retry(run_id="run_timeout")
    assert timeout_renderer.calls == 1

    fatal_renderer = _FlakyRenderer(["REALS_QC_NOT_DELIVERABLE"], retryable=False)
    with pytest.raises(RendererError):
        _service(renderer=fatal_renderer)._render_with_retry(run_id="run_fatal")
    assert fatal_renderer.calls == 1


def test_validator_flags_caption_glyphs_missing_from_font(monkeypatch):
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[0].caption.text = "오늘만 딸기 크림 라떼\U0001f525"
    validator = EditRecipeValidator()
    supported = {
        ord(character)
        for clip in recipe.timeline
        if clip.caption is not None
        for character in clip.caption.text
    } | {ord(character) for character in recipe.cta.text}
    supported -= {ord("\U0001f525")}
    monkeypatch.setattr(validator.registry, "caption_font_cmap", lambda: supported)

    issues = validator.validate(
        recipe,
        selected_shortform=SelectedShortform(
            recommendation_id="rec_123",
            editing_template_id="video_editing_db_014",
            editing_template_version=3,
        ),
        video_editing_db=_fallback_video_editing_db([]),
        video_contexts=FakeVideoContextBuilder().build(_request().videos),
    )

    glyph_issues = [issue for issue in issues if issue.code == "CAPTION_GLYPH_UNSUPPORTED"]
    assert glyph_issues and "\U0001f525" in glyph_issues[0].message
    assert all(issue.repairable for issue in glyph_issues)


def _engine_typewriter_failures(*, speed: float, window_ms: int) -> list[str]:
    segment = RecipeSegment(
        recipe_segment_id="rs_001",
        produced_segment_id="ps_001",
        sequence_index=1,
        trim_in_ms=0,
        trim_out_ms=window_ms,
        speed_multiplier=speed,
    )
    overlay = Overlay(
        overlay_id="ov_typewriter",
        produced_segment_id="ps_001",
        overlay_type=OverlayType.CAPTION,
        text_content="할인 중",
        style_id="CAPTION",
        start_ms=0,
        end_ms=window_ms,
        motion_id=MotionId.TYPEWRITER,
    )
    recipe = EngineEditRecipe(
        recipe_id="recipe_typewriter",
        produced_video_id="pv_001",
        segments=[segment],
        overlays=[overlay],
    )
    produced = MediaFileRef(
        file_id="pv_001",
        path="unused.mp4",
        duration_ms=window_ms,
        width=1080,
        height=1920,
        fps=30.0,
    )
    try:
        validate_recipe(recipe, produced, Registries(str(ENGINE_ROOT)))
    except ValidationError as exc:
        return [failure for failure in exc.failures if "TYPEWRITER 노출시간" in failure]
    return []


def test_engine_typewriter_minimum_uses_visible_output_time():
    # "할인 중" = 3 units -> 2*80+600 = 760ms required visible time.
    # A 700ms produced window at speed 0.5 plays for 1400ms, so it must pass;
    # the same window at speed 1.0 plays for 700ms and must fail.
    assert _engine_typewriter_failures(speed=1.0, window_ms=700)
    assert not _engine_typewriter_failures(speed=0.5, window_ms=700)


def test_build_concat_plan_can_drop_audio_for_mixed_sources(tmp_path):
    profile = {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "pix_fmt": "yuv420p",
        "video_codec": "libx264",
        "crf": 20,
        "preset": "veryfast",
        "x264_profile": "high",
        "level": "4.1",
        "gop": 60,
        "movflags": "+faststart",
        "audio_codec": "aac",
        "audio_bitrate": "128k",
        "audio_sample_rate": 44100,
    }
    cuts = [("a.mp4", 0.0, 1.0), ("b.mp4", 0.0, 1.0)]

    silent_commands, _ = build_concat_plan(
        cuts, str(tmp_path / "out.mp4"), profile, str(tmp_path), keep_audio=False
    )
    for command in silent_commands:
        assert "-an" in command
        assert "-af" not in command
        assert "-c:a" not in command

    default_commands, _ = build_concat_plan(
        cuts, str(tmp_path / "out2.mp4"), profile, str(tmp_path)
    )
    assert "-af" in default_commands[0]
    assert all("-an" not in command for command in default_commands)
