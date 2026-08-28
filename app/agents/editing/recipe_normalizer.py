from __future__ import annotations

from typing import Any

from app.agents.editing.reals import RealsRegistry, get_reals_registry
from app.schemas.editing import EditRecipe, RecipeCaption, RecipeClip, RecipeEffect


def normalize_recipe_for_rendering(
    recipe: EditRecipe,
    *,
    registry: RealsRegistry | None = None,
) -> EditRecipe:
    """Canonicalize LLM output to the REALS time and parameter contracts.

    Captions use absolute produced-timeline milliseconds. Timed effect windows
    use milliseconds relative to the clip that owns the effect. Invalid optional
    effects are removed instead of failing the entire render.
    """

    active_registry = registry or get_reals_registry()
    timeline = [_normalize_clip(clip, registry=active_registry) for clip in recipe.timeline]
    return recipe.model_copy(update={"timeline": timeline})


def _normalize_clip(clip: RecipeClip, *, registry: RealsRegistry) -> RecipeClip:
    duration_ms = max(
        1,
        int((clip.source_end_ms - clip.source_start_ms) / clip.speed),
    )
    caption = (
        _normalize_caption(clip.caption, clip=clip, duration_ms=duration_ms)
        if clip.caption is not None
        else None
    )
    effects = [
        normalized
        for effect in clip.effects
        if (
            normalized := _normalize_effect(
                effect,
                clip=clip,
                duration_ms=duration_ms,
                registry=registry,
            )
        )
        is not None
    ]
    return clip.model_copy(update={"caption": caption, "effects": effects})


def _normalize_caption(
    caption: RecipeCaption,
    *,
    clip: RecipeClip,
    duration_ms: int,
) -> RecipeCaption:
    clip_start = clip.timeline_start_ms
    clip_end = clip_start + duration_ms
    start = caption.start_ms
    end = caption.end_ms
    requested_duration = max(1, end - start)

    # A frequent LLM error is returning clip-relative caption times even though
    # the caption contract is absolute produced-timeline time.
    if clip_start > 0 and 0 <= start < end <= duration_ms:
        start += clip_start
        end += clip_start

    if end <= clip_start or start >= clip_end:
        start = clip_start
        end = min(clip_end, clip_start + requested_duration)
    else:
        start = max(clip_start, min(start, clip_end - 1))
        end = max(start + 1, min(end, clip_end))

    motion_id = caption.motion_id
    if motion_id == "TYPEWRITER":
        units = len("".join(caption.text.split()))
        required_ms = max(0, units - 1) * 80 + 600
        if required_ms <= duration_ms:
            end = min(clip_end, max(end, start + required_ms))
            if end - start < required_ms:
                start = max(clip_start, end - required_ms)
        else:
            motion_id = "POP"

    return caption.model_copy(
        update={
            "start_ms": int(start),
            "end_ms": int(end),
            "motion_id": motion_id,
            "scale": 1.0,
        }
    )


def _normalize_effect(
    effect: RecipeEffect,
    *,
    clip: RecipeClip,
    duration_ms: int,
    registry: RealsRegistry,
) -> RecipeEffect | None:
    rules = registry.effect_rules(effect.effect_id)
    if rules is None:
        return None
    allowed = rules.get("allowed_params") or {}
    raw = effect.params.model_dump(exclude_none=True)

    # PUNCH_ZOOM is intentionally not a timed effect. Preserve a usable zoom
    # target when the model incorrectly emits the richer ZOOM parameter shape.
    if effect.effect_id == "PUNCH_ZOOM" and raw.get("scale_end") is None:
        for alias in ("scale", "scale_start"):
            if raw.get(alias) is not None:
                raw["scale_end"] = raw[alias]
                break

    params = {name: raw[name] for name in allowed if raw.get(name) is not None}
    if "start_ms" in allowed and "end_ms" in allowed:
        window = _normalize_effect_window(
            params.get("start_ms"),
            params.get("end_ms"),
            clip_start_ms=clip.timeline_start_ms,
            duration_ms=duration_ms,
        )
        if window is None:
            return None
        params["start_ms"], params["end_ms"] = window

    for name, rule in allowed.items():
        if name not in params:
            return None
        value = params[name]
        if "enum" in rule:
            if value not in rule["enum"]:
                return None
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if "min" in rule:
            numeric = max(float(rule["min"]), numeric)
        if "max" in rule:
            numeric = min(float(rule["max"]), numeric)
        params[name] = int(numeric) if isinstance(value, int) else numeric

    return RecipeEffect.model_validate({"effect_id": effect.effect_id, "params": params})


def _normalize_effect_window(
    start_value: Any,
    end_value: Any,
    *,
    clip_start_ms: int,
    duration_ms: int,
) -> tuple[int, int] | None:
    try:
        start = int(start_value)
        end = int(end_value)
    except (TypeError, ValueError):
        return None

    # Convert the common absolute produced-timeline form to the renderer's
    # clip-relative effect window.
    if clip_start_ms > 0 and start >= clip_start_ms and (start >= duration_ms or end > duration_ms):
        start -= clip_start_ms
        end -= clip_start_ms

    if end <= 0 or start >= duration_ms:
        return None
    start = max(0, min(start, duration_ms - 1))
    end = max(0, min(end, duration_ms))
    if start >= end:
        return None
    return start, end
