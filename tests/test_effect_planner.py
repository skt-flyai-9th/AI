from __future__ import annotations

from app.agents.editing.effect_planner import EffectPlanner
from app.agents.editing.types import EditingPlanDecision, VideoContext
from app.agents.editing.validator import EditRecipeValidator
from app.core.config import Settings
from app.schemas.editing import (
    EditRecipe,
    PublishingResult,
    PublishingTrack,
    RecipeClip,
    RecipeCta,
    SelectedShortform,
)


def _recipe(*clips: RecipeClip) -> EditRecipe:
    return EditRecipe(
        editing_template_id="template-1",
        editing_template_version=1,
        timeline=list(clips),
        cta=RecipeCta(text="지금 확인해 보세요"),
    )


def _clip(
    *,
    order: int = 1,
    video_id: str = "video-1",
    source_start_ms: int = 0,
    source_end_ms: int = 2_000,
    timeline_start_ms: int = 0,
    speed: float = 1.0,
) -> RecipeClip:
    return RecipeClip(
        clip_order=order,
        video_id=video_id,
        source_start_ms=source_start_ms,
        source_end_ms=source_end_ms,
        timeline_start_ms=timeline_start_ms,
        speed=speed,
    )


def _planner(**kwargs: object) -> EffectPlanner:
    return EffectPlanner(
        settings=Settings(editing_disabled_effect_ids=""),
        **kwargs,
    )


def test_adds_effects_from_frame_evidence() -> None:
    recipe = _recipe(_clip())

    planned = _planner().apply_recipe(
        recipe,
        produced_frame_context={
            "observations": [
                {
                    "video_id": "video-1",
                    "timestamp_ms": 700,
                    "semantic_event": "PRODUCT_REVEAL",
                    "motion_strength": 0.2,
                    "flash_level": 0.8,
                }
            ]
        },
        video_editing_db={"editing_rules": {"allowed_effect_ids": []}},
    )

    effects = {effect.effect_id: effect for effect in planned.timeline[0].effects}
    assert set(effects) == {"PUNCH_ZOOM", "FLASH"}
    assert effects["PUNCH_ZOOM"].params.scale_end == 1.07
    assert effects["FLASH"].params.start_ms == 675
    assert effects["FLASH"].params.end_ms == 765
    assert effects["FLASH"].params.opacity == 0.8


def test_maps_source_timestamp_to_speed_adjusted_clip_time() -> None:
    recipe = _recipe(_clip(source_start_ms=1_000, source_end_ms=3_000, speed=2.0))

    planned = _planner().apply_recipe(
        recipe,
        produced_frame_context={
            "observations": [
                {
                    "video_id": "video-1",
                    "timestamp_ms": 1_400,
                    "semantic_event": "IMPACT_BEAT",
                    "motion_strength": 0.9,
                }
            ]
        },
        video_editing_db={"editing_rules": {"allowed_effect_ids": ["SHAKE"]}},
    )

    effect = planned.timeline[0].effects[0]
    assert effect.effect_id == "SHAKE"
    assert effect.params.start_ms == 200
    assert effect.params.end_ms == 420
    assert effect.params.amplitude_x_pct == 0.012
    assert effect.params.frequency_hz == 12.0
    assert effect.params.damping is True


def test_empty_effect_allowlist_uses_safe_baseline() -> None:
    recipe = _recipe(
        _clip(source_end_ms=1_000),
        _clip(
            order=2,
            video_id="video-2",
            source_end_ms=1_500,
            timeline_start_ms=1_000,
        ),
    )

    planned = _planner().apply_recipe(
        recipe,
        produced_frame_context={"observations": []},
        video_editing_db={"editing_rules": {"allowed_effect_ids": []}},
    )

    assert [effect.effect_id for effect in planned.timeline[0].effects] == ["PUNCH_ZOOM"]
    assert [effect.effect_id for effect in planned.timeline[1].effects] == ["ZOOM"]


def test_nonempty_effect_allowlist_restricts_planner() -> None:
    planned = _planner().apply_recipe(
        _recipe(_clip()),
        produced_frame_context={
            "observations": [
                {
                    "video_id": "video-1",
                    "timestamp_ms": 500,
                    "semantic_event": "PRODUCT_REVEAL",
                    "flash_level": 0.7,
                }
            ]
        },
        video_editing_db={"editing_rules": {"allowed_effect_ids": ["FLASH"]}},
    )

    assert [effect.effect_id for effect in planned.timeline[0].effects] == ["FLASH"]


def test_source_gap_decision_is_unchanged() -> None:
    decision = EditingPlanDecision(
        outcome="SOURCE_GAP",
        recipe=None,
        publishing=None,
        missing_scene_roles=["RESULT"],
        available_options=["USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"],
        rationale="결과 장면이 없습니다.",
    )

    assert (
        _planner().apply(
            decision,
            produced_frame_context={"observations": []},
            video_editing_db={},
        )
        is decision
    )


def test_validator_accepts_renderer_effects_for_empty_allowlist() -> None:
    recipe = _planner().apply_recipe(
        _recipe(_clip()),
        produced_frame_context={"observations": []},
        video_editing_db={"editing_rules": {"allowed_effect_ids": []}},
    )
    selected = SelectedShortform(
        recommendation_id="recommendation-1",
        editing_template_id="template-1",
        editing_template_version=1,
    )
    contexts = [
        VideoContext(
            video_id="video-1",
            shooting_scene_order=1,
            duration_ms=2_000,
            width=1080,
            height=1920,
            fps=30,
            keyframes=[],
        )
    ]

    issues = EditRecipeValidator(settings=Settings(editing_disabled_effect_ids="")).validate(
        recipe,
        selected_shortform=selected,
        video_editing_db={"editing_rules": {"allowed_effect_ids": []}},
        video_contexts=contexts,
    )

    assert not [issue for issue in issues if "EFFECT" in issue.code]


def test_recipe_decision_keeps_publishing_payload() -> None:
    decision = EditingPlanDecision(
        outcome="RECIPE",
        recipe=_recipe(_clip()),
        publishing=PublishingResult(
            title="신상품 소개",
            caption="새로 나온 상품을 소개합니다.",
            hashtags=["#신상품", "#상품소개", "#추천", "#쇼츠", "#릴스"],
            track=PublishingTrack(mode="FIXED", title="검증된 음원"),
        ),
        missing_scene_roles=[],
        available_options=[],
        rationale="촬영본으로 편집 가능합니다.",
    )

    planned = _planner().apply(
        decision,
        produced_frame_context={"observations": []},
        video_editing_db={},
    )

    assert planned.publishing == decision.publishing
    assert planned.recipe is not None
    assert planned.recipe.timeline[0].effects[0].effect_id == "PUNCH_ZOOM"
