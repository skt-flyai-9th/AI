from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from app.agents.editing import renderer as renderer_module
from app.agents.editing.reals import (
    RealsRecipeAdapter,
    RealsRegistry,
    RealsRegistryError,
)
from app.agents.editing.renderer import HttpEditingRenderer, RendererError
from app.agents.editing.types import VideoContext
from app.agents.editing.validator import EditRecipeValidator
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
        "video_editing_db_id": "video_editing_db_014",
        "video_editing_db_version": 3,
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
        RecipeEffect.model_validate(
            {"effect_id": "COLOR_TONE", "params": {"tone": "WARM"}}
        ),
        RecipeEffect.model_validate(
            {"effect_id": "PUNCH_ZOOM", "params": {"scale_end": 1.1}}
        ),
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
    assert [effect.effect_id for effect in final.edit_recipe.segments[0].effects] == [
        "PUNCH_ZOOM"
    ]
    caption, cta = final.edit_recipe.overlays
    assert (caption.start_ms, caption.end_ms) == (0, 1500)
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
        issue.code == "TIMELINE_NOT_GAPLESS"
        and issue.path == "timeline[0].timeline_start_ms"
        for issue in issues
    )
    by_code = {issue.code: issue for issue in issues}
    assert by_code["CAPTION_SCALE_UNSUPPORTED"].source == "REALS_REGISTRY"
    capabilities = validator.registry.llm_capabilities()
    assert set(capabilities["effects"]) == validator.registry.creative_effect_ids
    assert capabilities["max_caption_chars"] == 40


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
