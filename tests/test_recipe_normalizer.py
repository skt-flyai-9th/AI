from app.agents.editing.recipe_normalizer import normalize_recipe_for_rendering
from app.schemas.editing import EditRecipe


def _recipe(timeline: list[dict]) -> EditRecipe:
    return EditRecipe.model_validate(
        {
            "editing_template_id": "video_editing_db_001",
            "editing_template_version": 1,
            "timeline": timeline,
            "cta": {"text": "지금 확인해보세요"},
        }
    )


def test_normalizes_production_failures_before_renderer() -> None:
    recipe = _recipe(
        [
            {
                "clip_order": 1,
                "video_id": "cut_1",
                "source_start_ms": 0,
                "source_end_ms": 1900,
                "timeline_start_ms": 0,
                "caption": {
                    "text": "첫 번째 자막",
                    "start_ms": 2400,
                    "end_ms": 3100,
                },
                "effects": [
                    {
                        "effect_id": "PUNCH_ZOOM",
                        "params": {
                            "start_ms": 100,
                            "end_ms": 500,
                            "scale_start": 1.0,
                            "scale_end": 1.08,
                        },
                    }
                ],
            },
            {
                "clip_order": 2,
                "video_id": "cut_2",
                "source_start_ms": 0,
                "source_end_ms": 1900,
                "timeline_start_ms": 1900,
                "caption": {
                    "text": "두 번째 자막",
                    "start_ms": 100,
                    "end_ms": 900,
                },
                "effects": [
                    {
                        "effect_id": "FLASH",
                        "params": {
                            "start_ms": 2430,
                            "end_ms": 2475,
                            "opacity": 0.8,
                        },
                    }
                ],
            },
        ]
    )

    normalized = normalize_recipe_for_rendering(recipe)

    first = normalized.timeline[0]
    assert first.caption.start_ms == 0
    assert first.caption.end_ms == 700
    assert first.effects[0].params.model_dump(exclude_none=True) == {"scale_end": 1.08}

    second = normalized.timeline[1]
    assert second.caption.start_ms == 2000
    assert second.caption.end_ms == 2800
    assert second.effects[0].params.start_ms == 530
    assert second.effects[0].params.end_ms == 575


def test_drops_invalid_optional_effect_instead_of_failing_recipe() -> None:
    recipe = _recipe(
        [
            {
                "clip_order": 1,
                "video_id": "cut_1",
                "source_start_ms": 0,
                "source_end_ms": 1000,
                "timeline_start_ms": 0,
                "effects": [
                    {
                        "effect_id": "FLASH",
                        "params": {
                            "start_ms": 6500,
                            "end_ms": 6509,
                            "opacity": 0.8,
                        },
                    }
                ],
            }
        ]
    )

    normalized = normalize_recipe_for_rendering(recipe)

    assert normalized.timeline[0].effects == []
