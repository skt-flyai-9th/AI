from __future__ import annotations

from typing import Any

from app.agents.editing.types import VideoContext
from app.schemas.editing import EditRecipe, SelectedShortform


_RENDERER_EFFECTS = {"PUNCH_ZOOM", "COLOR_TONE", "SMOOTH_ZOOM"}
_RENDERER_TRANSITIONS = {None, "CUT", "HARD_CUT", "FLASH_WHITE"}


class EditRecipeValidator:
    """Deterministic hard-constraint gate before any recipe reaches Renderer."""

    def validate(
        self,
        recipe: EditRecipe,
        *,
        selected_shortform: SelectedShortform,
        template: dict[str, Any],
        video_contexts: list[VideoContext],
    ) -> list[str]:
        errors: list[str] = []
        if recipe.editing_template_id != selected_shortform.editing_template_id:
            errors.append("editing_template_id must match selected_shortform")
        if recipe.editing_template_version != selected_shortform.editing_template_version:
            errors.append("editing_template_version must match selected_shortform")

        rules = template.get("editing_rules") or {}
        configured_effects = rules.get("allowed_effect_ids")
        allowed_effects = (
            set(configured_effects)
            if isinstance(configured_effects, list)
            else set(_RENDERER_EFFECTS)
        )
        allowed_effects &= _RENDERER_EFFECTS
        configured_transitions = rules.get("allowed_transition_ids")
        allowed_transitions = (
            set(configured_transitions)
            if isinstance(configured_transitions, list)
            else set(_RENDERER_TRANSITIONS)
        )
        allowed_transitions &= _RENDERER_TRANSITIONS
        allowed_transitions.add(None)
        min_cut_ms = max(300, int(rules.get("min_cut_duration_ms") or 300))
        max_duration_ms = int(float(rules.get("max_duration_sec") or 90) * 1000)

        contexts = {context.video_id: context for context in video_contexts}
        order_by_video = {
            context.video_id: context.shooting_scene_order for context in video_contexts
        }
        clip_orders = [clip.clip_order for clip in recipe.timeline]
        expected_orders = list(range(1, len(recipe.timeline) + 1))
        if clip_orders != expected_orders:
            errors.append(f"clip_order must be consecutive: expected {expected_orders}")

        expected_start = 0.0
        scene_orders: list[int] = []
        caption_count = 0
        for clip in recipe.timeline:
            context = contexts.get(clip.video_id)
            if context is None:
                errors.append(f"clip {clip.clip_order}: unknown video_id={clip.video_id}")
                continue
            scene_orders.append(order_by_video[clip.video_id])
            if clip.source_start_ms >= clip.source_end_ms:
                errors.append(f"clip {clip.clip_order}: source_start_ms must be before source_end_ms")
                continue
            if clip.source_end_ms > context.duration_ms:
                errors.append(
                    f"clip {clip.clip_order}: source_end_ms exceeds {clip.video_id} duration"
                )
            source_duration = clip.source_end_ms - clip.source_start_ms
            output_duration = source_duration / clip.speed
            if output_duration < min_cut_ms:
                errors.append(f"clip {clip.clip_order}: output duration is below {min_cut_ms}ms")
            if abs(clip.timeline_start_ms - expected_start) > 1:
                errors.append(
                    f"clip {clip.clip_order}: timeline_start_ms must be {round(expected_start)}"
                )
            clip_end = clip.timeline_start_ms + output_duration
            expected_start = clip_end

            if clip.transition_in not in allowed_transitions:
                errors.append(f"clip {clip.clip_order}: unsupported transition_in")
            if clip.transition_out not in allowed_transitions:
                errors.append(f"clip {clip.clip_order}: unsupported transition_out")
            for effect in clip.effects:
                if effect.effect_id not in allowed_effects:
                    errors.append(
                        f"clip {clip.clip_order}: unsupported effect_id={effect.effect_id}"
                    )
                errors.extend(
                    _validate_effect_params(
                        clip.clip_order,
                        effect.effect_id,
                        effect.params.model_dump(exclude_none=True),
                    )
                )

            if clip.caption is not None:
                caption_count += 1
                caption = clip.caption
                if caption.start_ms >= caption.end_ms:
                    errors.append(f"clip {clip.clip_order}: caption start must be before end")
                if caption.start_ms < clip.timeline_start_ms or caption.end_ms > clip_end + 1:
                    errors.append(f"clip {clip.clip_order}: caption must stay inside clip timeline")
                if len(caption.text) > 40:
                    errors.append(f"clip {clip.clip_order}: caption exceeds 40 characters")

        if scene_orders != sorted(scene_orders):
            errors.append("timeline must preserve shooting_scene_order")
        if caption_count > 8:
            errors.append("recipe may contain at most 8 captions")
        if expected_start > max_duration_ms:
            errors.append(f"output duration exceeds {max_duration_ms}ms")
        return errors


def _validate_effect_params(clip_order: int, effect_id: str, params: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if effect_id in {"PUNCH_ZOOM", "SMOOTH_ZOOM"}:
        maximum = 1.15 if effect_id == "PUNCH_ZOOM" else 1.2
        scale = params.get("scale_end")
        if not isinstance(scale, (int, float)) or not 1.0 <= float(scale) <= maximum:
            errors.append(
                f"clip {clip_order}: {effect_id}.scale_end must be between 1.0 and {maximum}"
            )
    elif effect_id == "COLOR_TONE":
        if params.get("tone") not in {"NATURAL", "WARM", "COOL", "VIVID"}:
            errors.append(f"clip {clip_order}: COLOR_TONE.tone is invalid")
    return errors
