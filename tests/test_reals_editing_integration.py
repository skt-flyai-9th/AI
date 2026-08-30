from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from app.agents.editing import renderer as renderer_module
from app.agents.editing.llm import _renderer_capabilities
from app.agents.editing.reals import (
    RealsRecipeAdapter,
    RealsRegistry,
    RealsRegistryError,
)
from app.agents.editing.renderer import HttpEditingRenderer, RendererError
from app.agents.editing.types import VideoContext
from app.agents.editing.validator import EditRecipeValidator
from app.core.config import Settings
from app.schemas.editing import RecipeEffect, SelectedShortform
from tests.test_editing_agent import _recipe, _request


def _contexts() -> list[VideoContext]:
    return [
        VideoContext(
            video_id=video.video_id,
            shooting_scene_order=video.shooting_scene_order,
            duration_ms=5000,
            width=1080,
            height=1920,
            fps=30.0,
            keyframes=[],
        )
        for video in _request().videos
    ]


def _video_editing_db() -> dict:
    return {
        "editing_template_id": "video_editing_db_014",
        "editing_template_version": 3,
        "editing_rules": {
            "min_cut_duration_ms": 300,
            "max_duration_sec": 30,
            "allowed_effect_ids": ["PUNCH_ZOOM", "COLOR_TONE"],
            "allowed_transition_ids": ["CUT", "HARD_CUT"],
        },
    }


def test_reals_adapter_builds_multicut_assembly_and_engine_recipe():
    recipe = _recipe()
    recipe.timeline[0].effects = [
        RecipeEffect.model_validate({"effect_id": "COLOR_TONE", "params": {"tone": "WARM"}}),
        RecipeEffect.model_validate({"effect_id": "PUNCH_ZOOM", "params": {"scale_end": 1.1}}),
    ]
    request = RealsRecipeAdapter().build_request(
        run_id="edit_contract_1",
        recipe=recipe,
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )

    assert request.contract_version == "reals-render-job-1.0"
    assert request.idempotency_key.startswith("editing:edit_contract_1:")
    assert request.registry_manifest_sha256.startswith("sha256:")
    assert [asset.file_id for asset in request.source_assets] == ["take_501", "take_502"]
    assert request.source_assembly is not None
    assert [
        (segment.source_file_id, segment.trim_in_ms, segment.trim_out_ms)
        for segment in request.source_assembly.segments
    ] == [
        ("take_501", 500, 2500),
        ("take_502", 1000, 3000),
    ]

    final = request.final_render
    assert final.source_mode == "MULTI_CUT_ASSEMBLED"
    assert final.produced_video.duration_ms == 4000
    assert final.template_bundle_id == "tb_local_dev_001"
    assert final.edit_recipe.original_audio_policy == "REMOVE"
    assert final.edit_recipe.bgm_policy == "NONE"
    assert final.edit_recipe.final_audio_policy == "SILENT"
    assert [
        (segment.trim_in_ms, segment.trim_out_ms, segment.transition_id)
        for segment in final.edit_recipe.segments
    ] == [
        (0, 2000, "NONE"),
        (2000, 4000, "HARD_CUT"),
    ]
    assert final.edit_recipe.segments[0].color_tone == "WARM"
    assert [effect.effect_id for effect in final.edit_recipe.segments[0].effects] == ["PUNCH_ZOOM"]
    caption, reveal_caption = final.edit_recipe.overlays
    assert (caption.start_ms, caption.end_ms, caption.style_id) == (0, 1500, "HOOK")
    assert caption.motion_id == "TYPEWRITER"
    assert reveal_caption.style_id == "CAPTION_EMPHASIS"
    assert reveal_caption.motion_id == "POP"
    assert not any(overlay.overlay_id == "ov_cta" for overlay in final.edit_recipe.overlays)


def test_reals_adapter_passes_crop_center_through_and_defaults_to_center():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[0].crop_center_x = 0.25
    recipe.timeline[0].crop_center_y = 0.75
    assert recipe.timeline[1].crop_center_x is None
    assert recipe.timeline[1].crop_center_y is None

    request = RealsRecipeAdapter().build_request(
        run_id="edit_crop_center_1",
        recipe=recipe,
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )

    segments = request.final_render.edit_recipe.segments
    assert (segments[0].crop_center_x, segments[0].crop_center_y) == (0.25, 0.75)
    assert (segments[1].crop_center_x, segments[1].crop_center_y) == (0.5, 0.5)
    assert request.source_assembly is not None
    assembly_segments = request.source_assembly.segments
    assert (assembly_segments[0].crop_center_x, assembly_segments[0].crop_center_y) == (
        0.25,
        0.75,
    )
    assert (assembly_segments[1].crop_center_x, assembly_segments[1].crop_center_y) == (0.5, 0.5)


def test_reals_adapter_adds_cta_only_when_last_clip_has_no_overlapping_caption():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[-1].caption = None

    request = RealsRecipeAdapter().build_request(
        run_id="edit_non_overlapping_cta",
        recipe=recipe,
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )

    cta = next(
        overlay
        for overlay in request.final_render.edit_recipe.overlays
        if overlay.overlay_id == "ov_cta"
    )
    assert (cta.start_ms, cta.end_ms, cta.style_id) == (2000, 4000, "CTA_BOX")


def test_reals_adapter_uses_one_take_without_source_assembly():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline = [recipe.timeline[0]]

    request = RealsRecipeAdapter().build_request(
        run_id="edit_one_take_1",
        recipe=recipe,
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )

    assert request.source_assembly is None
    assert request.final_render.source_mode == "ONE_TAKE_PASSTHROUGH"
    assert request.final_render.produced_video.file_id == "take_501"
    assert request.final_render.produced_video.duration_ms == 5000
    segment = request.final_render.edit_recipe.segments[0]
    assert (segment.trim_in_ms, segment.trim_out_ms) == (500, 2500)
    caption = request.final_render.edit_recipe.overlays[0]
    assert (caption.start_ms, caption.end_ms) == (500, 2000)


def test_reals_adapter_maps_output_time_back_to_produced_time_with_speed():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[0].speed = 2.0
    recipe.timeline[1].timeline_start_ms = 1000
    recipe.timeline[0].caption.end_ms = 1000

    request = RealsRecipeAdapter().build_request(
        run_id="edit_speed_1",
        recipe=recipe,
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )

    caption = request.final_render.edit_recipe.overlays[0]
    assert (caption.start_ms, caption.end_ms) == (0, 2000)
    assert request.final_render.edit_recipe.segments[0].speed_multiplier == 2.0


def test_validator_returns_structured_issues_from_reals_registry():
    recipe = _recipe(invalid_timeline=True).model_copy(deep=True)
    recipe.timeline[0].caption.scale = 1.2
    validator = EditRecipeValidator()

    issues = validator.validate(
        recipe,
        selected_shortform=SelectedShortform.model_validate(
            _request().selected_shortform.model_dump(mode="json")
        ),
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
    )

    assert any(
        issue.code == "TIMELINE_NOT_GAPLESS" and issue.path == "timeline[0].timeline_start_ms"
        for issue in issues
    )
    by_code = {issue.code: issue for issue in issues}
    assert by_code["CAPTION_SCALE_UNSUPPORTED"].source == "REALS_REGISTRY"
    capabilities = validator.registry.llm_capabilities()
    assert set(capabilities["effects"]) == validator.registry.creative_effect_ids
    assert capabilities["max_caption_chars"] == 40
    assert "TYPEWRITER" in capabilities["caption_motion_ids"]


def test_validator_rejects_effect_window_outside_host_clip():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[0].effects = [
        RecipeEffect.model_validate(
            {
                "effect_id": "FLASH",
                "params": {
                    "start_ms": 1800,
                    "end_ms": 2100,
                    "opacity": 0.8,
                },
            }
        )
    ]
    video_editing_db = _video_editing_db()
    video_editing_db["editing_rules"]["allowed_effect_ids"].append("FLASH")

    issues = EditRecipeValidator().validate(
        recipe,
        selected_shortform=SelectedShortform.model_validate(
            _request().selected_shortform.model_dump(mode="json")
        ),
        video_editing_db=video_editing_db,
        video_contexts=_contexts(),
    )

    assert any(issue.code == "EFFECT_WINDOW_OUTSIDE_CLIP" for issue in issues)


def test_validator_rejects_promotional_video_without_regular_captions():
    recipe = _recipe().model_copy(deep=True)
    for clip in recipe.timeline:
        clip.caption = None
    validator = EditRecipeValidator()

    issues = validator.validate(
        recipe,
        selected_shortform=_request().selected_shortform,
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=_request().project.model_dump(mode="json"),
    )

    codes = {issue.code for issue in issues}
    assert "PROMOTIONAL_CAPTIONS_MISSING" in codes
    assert "PROMOTIONAL_HOOK_MISSING" in codes
    assert "PROMOTIONAL_REVEAL_CAPTION_MISSING" in codes


def test_validator_allows_single_clip_promotional_recipe_without_reveal_caption():
    # With one usable clip (e.g. the ordered fallback after SOURCE_GAP) the
    # only caption slot is claimed by the HOOK, so requiring CAPTION_EMPHASIS
    # would make every single-clip promotional recipe unsatisfiable.
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline = recipe.timeline[:1]
    validator = EditRecipeValidator()

    issues = validator.validate(
        recipe,
        selected_shortform=_request().selected_shortform,
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=_request().project.model_dump(mode="json"),
    )

    codes = {issue.code for issue in issues}
    assert "PROMOTIONAL_REVEAL_CAPTION_MISSING" not in codes
    assert "PROMOTIONAL_CAPTIONS_MISSING" not in codes
    assert "PROMOTIONAL_HOOK_MISSING" not in codes


def _project_with_copy_directives(**directives) -> dict:
    return {"shortform_context": {"copy_directives": directives}}


def test_validator_flags_caption_too_short_to_read():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[1].caption.end_ms = recipe.timeline[1].caption.start_ms + 100

    issues = EditRecipeValidator().validate(
        recipe,
        selected_shortform=SelectedShortform.model_validate(
            _request().selected_shortform.model_dump(mode="json")
        ),
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
    )

    assert any(issue.code == "CAPTION_DURATION_TOO_SHORT" for issue in issues)


def test_validator_enforces_requested_caption_position():
    recipe = _recipe().model_copy(deep=True)
    project = _project_with_copy_directives(caption_position_request="TOP")

    issues = EditRecipeValidator().validate(
        recipe,
        selected_shortform=SelectedShortform.model_validate(
            _request().selected_shortform.model_dump(mode="json")
        ),
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=project,
    )

    mismatches = [issue for issue in issues if issue.code == "PROJECT_CAPTION_POSITION_MISMATCH"]
    assert len(mismatches) == 2


def test_validator_enforces_requested_min_caption_duration():
    recipe = _recipe().model_copy(deep=True)
    project = _project_with_copy_directives(requested_min_caption_ms=5000)

    issues = EditRecipeValidator().validate(
        recipe,
        selected_shortform=SelectedShortform.model_validate(
            _request().selected_shortform.model_dump(mode="json")
        ),
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=project,
    )

    shortfalls = [
        issue for issue in issues if issue.code == "PROJECT_CAPTION_DURATION_TOO_SHORT"
    ]
    assert len(shortfalls) == 2


def test_validator_requires_project_scoped_verbatim_caption_phrase():
    recipe = _recipe().model_copy(deep=True)
    project = _request().project.model_dump(mode="json")
    project["shortform_context"] = {"copy_directives": {"verbatim_caption_phrases": ["딸기청 톡!"]}}
    validator = EditRecipeValidator()

    missing = validator.validate(
        recipe,
        selected_shortform=_request().selected_shortform,
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=project,
    )
    assert any(issue.code == "PROJECT_CAPTION_PHRASE_MISSING" for issue in missing)

    recipe.timeline[0].caption.text = "딸기청 톡!"
    included = validator.validate(
        recipe,
        selected_shortform=_request().selected_shortform,
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=project,
    )
    assert not any(issue.code == "PROJECT_CAPTION_PHRASE_MISSING" for issue in included)


def test_validator_rejects_stage_directions_as_promotional_captions():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[1].caption.text = "손바닥 클로즈업으로 전환"

    issues = EditRecipeValidator().validate(
        recipe,
        selected_shortform=_request().selected_shortform,
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=_request().project.model_dump(mode="json"),
    )

    assert any(issue.code == "PROMOTIONAL_CAPTION_IS_STAGE_DIRECTION" for issue in issues)


def test_validator_allows_explicit_verbatim_stage_direction_caption():
    recipe = _recipe().model_copy(deep=True)
    phrase = "손바닥 클로즈업으로 전환"
    recipe.timeline[1].caption.text = phrase
    project = _request().project.model_dump(mode="json")
    project["shortform_context"] = {"copy_directives": {"verbatim_caption_phrases": [phrase]}}

    issues = EditRecipeValidator().validate(
        recipe,
        selected_shortform=_request().selected_shortform,
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=project,
    )

    assert not any(issue.code == "PROMOTIONAL_CAPTION_IS_STAGE_DIRECTION" for issue in issues)


def test_validator_requires_promotion_subject_in_first_hook():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[0].caption.text = "오늘의 대표 메뉴"

    issues = EditRecipeValidator().validate(
        recipe,
        selected_shortform=_request().selected_shortform,
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=_request().project.model_dump(mode="json"),
    )

    assert any(issue.code == "PROMOTIONAL_HOOK_NOT_PERSONALIZED" for issue in issues)


def test_validator_rejects_typewriter_without_animation_hold_time():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[0].caption.end_ms = 500
    validator = EditRecipeValidator()

    issues = validator.validate(
        recipe,
        selected_shortform=_request().selected_shortform,
        video_editing_db=_video_editing_db(),
        video_contexts=_contexts(),
        project=_request().project.model_dump(mode="json"),
    )

    assert any(issue.code == "TYPEWRITER_CAPTION_TOO_SHORT" for issue in issues)


def test_typewriter_ass_reveals_complete_korean_characters():
    engine_root = Path(__file__).resolve().parents[1] / "reals-video-engine"
    if str(engine_root) not in sys.path:
        sys.path.insert(0, str(engine_root))

    from reals_edit_engine.contracts import (
        FontWeight,
        MotionId,
        Overlay,
        OverlayType,
        PlacementId,
    )
    from reals_edit_engine.subtitle_layout import PlacedOverlay, _graphemes, build_ass

    assert _graphemes("가") == ["가"]
    overlay = Overlay(
        overlay_id="ov_typewriter",
        produced_segment_id="ps_001",
        overlay_type=OverlayType.CAPTION,
        text_content="홍대 맛집",
        style_id="HOOK",
        start_ms=0,
        end_ms=1500,
        placement_id=PlacementId.UPPER_SAFE,
        motion_id=MotionId.TYPEWRITER,
        font_weight=FontWeight.BOLD,
    )
    placed = PlacedOverlay(
        overlay=overlay,
        out_start_ms=0,
        out_end_ms=1500,
        x=540,
        y=400,
        font_px=92,
        lines=["홍대 맛집"],
    )

    class StubFontRegistry:
        @staticmethod
        def resolve_font(_font_asset_id: str, _weight: str) -> dict[str, object]:
            return {"ass_family": "Pretendard", "ass_bold": -1}

    ass = build_ass([placed], StubFontRegistry())
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue:")]

    assert len(dialogues) == 4
    assert "0:00:00.00,0:00:00.08" in dialogues[0]
    assert "{\\alpha&HFF&\\3a&HFF&}" in dialogues[0]
    assert dialogues[-1].endswith("홍대 맛집")


def test_free_tier_profile_limits_duration_and_disables_heavy_effect():
    recipe = _recipe().model_copy(deep=True)
    recipe.timeline[0].source_start_ms = 0
    recipe.timeline[0].source_end_ms = 8000
    recipe.timeline[1].source_start_ms = 0
    recipe.timeline[1].source_end_ms = 8000
    recipe.timeline[1].timeline_start_ms = 8000
    recipe.timeline[0].effects = [
        RecipeEffect.model_validate({"effect_id": "SMOOTH_ZOOM", "params": {"scale_end": 1.08}})
    ]
    video_editing_db = _video_editing_db()
    video_editing_db["editing_rules"]["allowed_effect_ids"].append("SMOOTH_ZOOM")
    contexts = [context.model_copy(update={"duration_ms": 20_000}) for context in _contexts()]
    settings = Settings(
        editing_max_output_duration_seconds=15,
        editing_disabled_effect_ids="SMOOTH_ZOOM",
    )

    issues = EditRecipeValidator(settings=settings).validate(
        recipe,
        selected_shortform=SelectedShortform.model_validate(
            _request().selected_shortform.model_dump(mode="json")
        ),
        video_editing_db=video_editing_db,
        video_contexts=contexts,
    )

    assert {"OUTPUT_TOO_LONG", "EFFECT_UNSUPPORTED"} <= {issue.code for issue in issues}


def test_llm_capabilities_publish_free_tier_envelope():
    capabilities = _renderer_capabilities(
        Settings(
            editing_max_videos_per_run=6,
            editing_max_output_duration_seconds=15,
            editing_disabled_effect_ids="SMOOTH_ZOOM",
        )
    )

    assert capabilities["max_input_videos"] == 6
    assert capabilities["max_output_duration_sec"] == 15
    assert "SMOOTH_ZOOM" not in capabilities["effects"]
    assert "SMOOTH_ZOOM" not in capabilities["effect_contracts"]
    assert capabilities["effect_contracts"]["PUNCH_ZOOM"] == {
        "required_params": ["scale_end"],
        "allowed_params": {"scale_end": {"min": 1.0, "max": 1.15}},
        "time_basis": "UNTIMED",
    }
    assert capabilities["effect_contracts"]["FLASH"]["time_basis"] == ("CLIP_RELATIVE_MS")


def test_registry_rejects_drift_from_manifest():
    source = RealsRegistry().registry_dir
    runtime_dir = Path("runtime-data")
    runtime_dir.mkdir(exist_ok=True)
    test_root = Path(tempfile.mkdtemp(prefix="registry-drift-", dir=runtime_dir))
    registry_copy = test_root / "registry"
    try:
        shutil.copytree(source, registry_copy)
        effect_path = registry_copy / "effect_registry.json"
        payload = json.loads(effect_path.read_text(encoding="utf-8"))
        payload["effects"].pop("PUNCH_ZOOM")
        effect_path.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(RealsRegistryError, match="checksum mismatch"):
            RealsRegistry(registry_copy)
    finally:
        shutil.rmtree(test_root)


class _FakeHttpClient:
    def __init__(self, response: httpx.Response, captured: dict) -> None:
        self.response = response
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, *, headers, json):
        self.captured.update({"url": url, "headers": headers, "json": json})
        return self.response


def test_http_renderer_posts_reals_contract(monkeypatch):
    captured: dict = {}
    response = httpx.Response(
        200,
        json={
            "deliverable": True,
            "render": {
                "output_video_url": "https://cdn.example/final.mp4",
                "resolution": "1080x1920",
                "duration_sec": 4.0,
                "cover_image_url": None,
            },
        },
    )
    monkeypatch.setattr(
        renderer_module.httpx,
        "Client",
        lambda **kwargs: _FakeHttpClient(response, captured),
    )
    renderer = HttpEditingRenderer()
    renderer.url = "http://renderer:8080"

    result = renderer.render(
        run_id="edit_http_1",
        recipe=_recipe(),
        videos=_request().videos,
        video_contexts=_contexts(),
        video_editing_db=_video_editing_db(),
    )

    assert result.duration_sec == 4.0
    assert captured["url"] == "http://renderer:8080/renders"
    assert captured["json"]["contract_version"] == "reals-render-job-1.0"
    assert "recipe" not in captured["json"]
    assert captured["json"]["final_render"]["edit_recipe"]["bgm_policy"] == "NONE"


def test_http_renderer_maps_structured_validation_error(monkeypatch):
    response = httpx.Response(
        422,
        json={
            "code": "REALS_RECIPE_INVALID",
            "message": "Native validator blocked render.",
            "validation_errors": [
                {"code": "FONT_GLYPH_MISSING", "path": "overlays[0].text_content"}
            ],
        },
    )
    monkeypatch.setattr(
        renderer_module.httpx,
        "Client",
        lambda **kwargs: _FakeHttpClient(response, {}),
    )
    renderer = HttpEditingRenderer()
    renderer.url = "http://renderer:8080"

    with pytest.raises(RendererError) as captured:
        renderer.render(
            run_id="edit_http_2",
            recipe=_recipe(),
            videos=_request().videos,
            video_contexts=_contexts(),
            video_editing_db=_video_editing_db(),
        )

    assert captured.value.code == "REALS_RECIPE_INVALID"
    assert captured.value.retryable is False
    assert captured.value.details[0]["code"] == "FONT_GLYPH_MISSING"
