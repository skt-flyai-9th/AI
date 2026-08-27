from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.editing.reals import RealsRegistry, get_reals_registry
from app.agents.editing.types import EditingPlanDecision
from app.core.config import Settings, get_settings
from app.schemas.editing import EditRecipe, RecipeClip, RecipeEffect


SAFE_AUTOMATIC_EFFECT_IDS = frozenset(
    {
        "COLOR",
        "COLOR_TONE",
        "FLASH",
        "POSITION_MOVE",
        "PUNCH_ZOOM",
        "ROTATION",
        "SHAKE",
        "VIBRATION",
        "ZOOM",
    }
)

_STRONG_EFFECT_IDS = frozenset({"FLASH", "SHAKE", "VIBRATION"})
_EFFECT_FAMILIES = {
    "PUNCH_ZOOM": "ZOOM",
    "ZOOM": "ZOOM",
    "SHAKE": "MOTION",
    "VIBRATION": "MOTION",
    "ROTATION": "MOTION",
    "POSITION_MOVE": "MOTION",
    "COLOR": "COLOR",
    "COLOR_TONE": "COLOR",
    "FLASH": "FLASH",
}


@dataclass(frozen=True)
class _EffectCandidate:
    effect_id: str
    params: dict[str, Any]
    score: float
    relative_ms: int


class EffectPlanner:
    """Deterministically add evidence-backed effects to an LLM edit recipe."""

    def __init__(
        self,
        *,
        registry: RealsRegistry | None = None,
        settings: Settings | None = None,
        max_effects_per_clip: int = 2,
        max_strong_effects_per_video: int = 3,
        min_flash_interval_ms: int = 800,
    ) -> None:
        self.registry = registry or get_reals_registry()
        self.settings = settings or get_settings()
        self.max_effects_per_clip = max_effects_per_clip
        self.max_strong_effects_per_video = max_strong_effects_per_video
        self.min_flash_interval_ms = min_flash_interval_ms

    def apply(
        self,
        decision: EditingPlanDecision,
        *,
        produced_frame_context: dict[str, Any],
        video_editing_db: dict[str, Any],
    ) -> EditingPlanDecision:
        if decision.outcome != "RECIPE" or decision.recipe is None:
            return decision
        recipe = self.apply_recipe(
            decision.recipe,
            produced_frame_context=produced_frame_context,
            video_editing_db=video_editing_db,
        )
        return decision.model_copy(update={"recipe": recipe})

    def apply_recipe(
        self,
        recipe: EditRecipe,
        *,
        produced_frame_context: dict[str, Any],
        video_editing_db: dict[str, Any],
    ) -> EditRecipe:
        allowed = self._allowed_effects(video_editing_db)
        if not allowed:
            return recipe

        observations = [
            item
            for item in (produced_frame_context.get("observations") or [])
            if isinstance(item, dict)
        ]
        candidates_by_clip = [
            self._candidates_for_clip(clip, observations, allowed) for clip in recipe.timeline
        ]
        if not any(candidates_by_clip):
            self._add_safe_baseline_candidates(recipe.timeline, candidates_by_clip, allowed)

        strong_count = sum(
            effect.effect_id in _STRONG_EFFECT_IDS
            for clip in recipe.timeline
            for effect in clip.effects
        )
        last_flash_ms: int | None = None
        timeline: list[RecipeClip] = []
        for clip, candidates in zip(recipe.timeline, candidates_by_clip, strict=True):
            effects = list(clip.effects)
            families = {
                _EFFECT_FAMILIES.get(effect.effect_id, effect.effect_id) for effect in effects
            }
            seen = {
                (
                    effect.effect_id,
                    effect.params.start_ms,
                    effect.params.end_ms,
                )
                for effect in effects
            }
            for candidate in sorted(candidates, key=lambda item: item.score, reverse=True):
                if len(effects) >= self.max_effects_per_clip:
                    break
                family = _EFFECT_FAMILIES[candidate.effect_id]
                if family in families:
                    continue
                is_strong = candidate.effect_id in _STRONG_EFFECT_IDS
                if is_strong and strong_count >= self.max_strong_effects_per_video:
                    continue
                absolute_ms = clip.timeline_start_ms + candidate.relative_ms
                if (
                    candidate.effect_id == "FLASH"
                    and last_flash_ms is not None
                    and absolute_ms - last_flash_ms < self.min_flash_interval_ms
                ):
                    continue
                key = (
                    candidate.effect_id,
                    candidate.params.get("start_ms"),
                    candidate.params.get("end_ms"),
                )
                if key in seen:
                    continue
                effects.append(
                    RecipeEffect.model_validate(
                        {"effect_id": candidate.effect_id, "params": candidate.params}
                    )
                )
                families.add(family)
                seen.add(key)
                if is_strong:
                    strong_count += 1
                if candidate.effect_id == "FLASH":
                    last_flash_ms = absolute_ms
            timeline.append(clip.model_copy(update={"effects": effects}))
        return recipe.model_copy(update={"timeline": timeline})

    def _allowed_effects(self, video_editing_db: dict[str, Any]) -> set[str]:
        renderer_effects = self.registry.creative_effect_ids & SAFE_AUTOMATIC_EFFECT_IDS
        configured = (video_editing_db.get("editing_rules") or {}).get("allowed_effect_ids")
        # Existing generated templates used [] as a placeholder rather than an
        # intentional opt-out. Treat it as the safe renderer set at runtime.
        allowed = set(configured) & renderer_effects if configured else set(renderer_effects)
        return allowed - self.settings.editing_disabled_effect_ids_set

    def _candidates_for_clip(
        self,
        clip: RecipeClip,
        observations: list[dict[str, Any]],
        allowed: set[str],
    ) -> list[_EffectCandidate]:
        duration_ms = max(1, int((clip.source_end_ms - clip.source_start_ms) / clip.speed))
        candidates: list[_EffectCandidate] = []
        tones: list[str] = []
        for observation in observations:
            if str(observation.get("video_id") or "") != clip.video_id:
                continue
            timestamp_ms = int(observation.get("timestamp_ms") or 0)
            if not clip.source_start_ms <= timestamp_ms <= clip.source_end_ms:
                continue
            relative_ms = int((timestamp_ms - clip.source_start_ms) / clip.speed)
            relative_ms = min(max(0, relative_ms), duration_ms - 1)
            candidates.extend(
                self._observation_candidates(
                    observation,
                    relative_ms=relative_ms,
                    duration_ms=duration_ms,
                    allowed=allowed,
                )
            )
            tone = str(observation.get("color_tone") or "UNKNOWN").upper()
            if tone in {"WARM", "COOL", "VIVID"}:
                tones.append(tone)

        if "COLOR_TONE" in allowed and len(tones) >= 2:
            tone = max(set(tones), key=tones.count)
            if tones.count(tone) >= 2:
                candidates.append(
                    _EffectCandidate(
                        effect_id="COLOR_TONE",
                        params={"tone": tone},
                        score=0.76,
                        relative_ms=0,
                    )
                )
        return _deduplicate_candidates(candidates)

    def _observation_candidates(
        self,
        observation: dict[str, Any],
        *,
        relative_ms: int,
        duration_ms: int,
        allowed: set[str],
    ) -> list[_EffectCandidate]:
        text = " ".join(
            str(observation.get(name) or "")
            for name in ("semantic_event", "action", "camera_motion", "motion_direction")
        ).upper()
        motion = _bounded_float(observation.get("motion_strength"), 0.0, 1.0)
        rotation = _bounded_float(observation.get("observed_rotation_deg"), -3.0, 3.0)
        zoom = _bounded_float(observation.get("observed_zoom_scale"), 0.5, 2.0, 1.0)
        translate_x = _bounded_float(observation.get("observed_translate_x_pct"), -0.08, 0.08)
        translate_y = _bounded_float(observation.get("observed_translate_y_pct"), -0.08, 0.08)
        flash = _bounded_float(observation.get("flash_level"), 0.0, 1.0)
        tone = str(observation.get("color_tone") or "UNKNOWN").upper()
        result: list[_EffectCandidate] = []

        if "PUNCH_ZOOM" in allowed and _contains(
            text,
            "HOOK",
            "REVEAL",
            "RESULT",
            "CTA",
            "공개",
            "강조",
            "결과",
        ):
            result.append(
                _EffectCandidate(
                    "PUNCH_ZOOM",
                    {"scale_end": 1.1 if motion >= 0.5 else 1.07},
                    0.9,
                    relative_ms,
                )
            )

        impact = _contains(text, "IMPACT", "HIT", "BEAT", "SNAP", "충격", "타격")
        if impact and motion >= 0.65 and "SHAKE" in allowed:
            result.append(
                _EffectCandidate(
                    "SHAKE",
                    _shake_params(relative_ms, duration_ms, strong=True),
                    min(0.97, 0.82 + motion * 0.15),
                    relative_ms,
                )
            )
        elif impact and "VIBRATION" in allowed:
            result.append(
                _EffectCandidate(
                    "VIBRATION",
                    _shake_params(relative_ms, duration_ms, strong=False),
                    min(0.9, 0.74 + motion * 0.12),
                    relative_ms,
                )
            )

        if "ROTATION" in allowed and (
            abs(rotation) >= 0.4 or _contains(text, "ROTAT", "TILT", "회전", "기울")
        ):
            start, end = _window(relative_ms, duration_ms, 320)
            result.append(
                _EffectCandidate(
                    "ROTATION",
                    {
                        "start_ms": start,
                        "end_ms": end,
                        "rotation_deg": rotation or 0.8,
                        "scale": 1.02,
                    },
                    min(0.9, 0.72 + abs(rotation) / 20),
                    relative_ms,
                )
            )

        if "POSITION_MOVE" in allowed and (
            max(abs(translate_x), abs(translate_y)) >= 0.02
            or _contains(text, "SWIPE", "PAN", "MOVE", "이동", "스와이프")
        ):
            start, end = _window(relative_ms, duration_ms, 480)
            result.append(
                _EffectCandidate(
                    "POSITION_MOVE",
                    {
                        "start_ms": start,
                        "end_ms": end,
                        "translate_x_pct": translate_x or _direction_x(text),
                        "translate_y_pct": translate_y or _direction_y(text),
                        "scale": 1.025,
                    },
                    min(0.88, 0.7 + motion * 0.16),
                    relative_ms,
                )
            )

        if "ZOOM" in allowed and (
            abs(zoom - 1.0) >= 0.025 or _contains(text, "ZOOM", "확대", "축소")
        ):
            start, end = _window(relative_ms, duration_ms, 700, lead_ms=100)
            target = min(1.14, max(1.04, zoom if zoom >= 1 else 2 - zoom))
            result.append(
                _EffectCandidate(
                    "ZOOM",
                    {
                        "start_ms": start,
                        "end_ms": end,
                        "scale_start": 1.0,
                        "scale_end": target,
                    },
                    min(0.87, 0.7 + abs(zoom - 1.0)),
                    relative_ms,
                )
            )

        if "FLASH" in allowed and (flash >= 0.45 or _contains(text, "FLASH", "플래시")):
            start, end = _window(relative_ms, duration_ms, 90, lead_ms=25)
            result.append(
                _EffectCandidate(
                    "FLASH",
                    {
                        "start_ms": start,
                        "end_ms": end,
                        "opacity": max(0.45, min(0.8, flash)),
                    },
                    min(0.98, 0.76 + flash * 0.2),
                    relative_ms,
                )
            )

        if "COLOR" in allowed and tone in {"WARM", "COOL", "VIVID"}:
            start, end = _window(relative_ms, duration_ms, 600, lead_ms=100)
            result.append(
                _EffectCandidate(
                    "COLOR",
                    {"start_ms": start, "end_ms": end, "tone": tone},
                    0.69,
                    relative_ms,
                )
            )
        return result

    @staticmethod
    def _add_safe_baseline_candidates(
        clips: list[RecipeClip],
        candidates_by_clip: list[list[_EffectCandidate]],
        allowed: set[str],
    ) -> None:
        if not clips:
            return
        if "PUNCH_ZOOM" in allowed and not clips[0].effects:
            candidates_by_clip[0].append(
                _EffectCandidate("PUNCH_ZOOM", {"scale_end": 1.06}, 0.6, 0)
            )
        if len(clips) < 2 or "ZOOM" not in allowed or clips[-1].effects:
            return
        duration_ms = max(
            1,
            int((clips[-1].source_end_ms - clips[-1].source_start_ms) / clips[-1].speed),
        )
        if duration_ms >= 400:
            candidates_by_clip[-1].append(
                _EffectCandidate(
                    "ZOOM",
                    {
                        "start_ms": 0,
                        "end_ms": min(duration_ms, 1200),
                        "scale_start": 1.0,
                        "scale_end": 1.05,
                    },
                    0.55,
                    0,
                )
            )


def _deduplicate_candidates(
    candidates: list[_EffectCandidate],
) -> list[_EffectCandidate]:
    best: dict[tuple[str, int], _EffectCandidate] = {}
    for candidate in candidates:
        key = (candidate.effect_id, candidate.relative_ms // 500)
        current = best.get(key)
        if current is None or candidate.score > current.score:
            best[key] = candidate
    return list(best.values())


def _shake_params(relative_ms: int, duration_ms: int, *, strong: bool) -> dict[str, Any]:
    start, end = _window(relative_ms, duration_ms, 220 if strong else 150)
    return {
        "start_ms": start,
        "end_ms": end,
        "amplitude_x_pct": 0.012 if strong else 0.006,
        "amplitude_y_pct": 0.006 if strong else 0.004,
        "rotation_deg": 0.4 if strong else 0.15,
        "scale": 1.018 if strong else 1.012,
        "frequency_hz": 12.0 if strong else 20.0,
        "damping": True,
    }


def _window(
    relative_ms: int,
    duration_ms: int,
    window_ms: int,
    *,
    lead_ms: int = 0,
) -> tuple[int, int]:
    start = min(max(0, relative_ms - lead_ms), max(0, duration_ms - 1))
    end = min(duration_ms, start + window_ms)
    if end <= start:
        end = min(duration_ms, start + 1)
    return start, end


def _contains(value: str, *markers: str) -> bool:
    return any(marker in value for marker in markers)


def _direction_x(value: str) -> float:
    if _contains(value, "LEFT", "좌", "왼"):
        return -0.035
    if _contains(value, "RIGHT", "우", "오른"):
        return 0.035
    return 0.025


def _direction_y(value: str) -> float:
    if _contains(value, "UP", "위", "상"):
        return -0.025
    if _contains(value, "DOWN", "아래", "하"):
        return 0.025
    return 0.0


def _bounded_float(
    value: Any,
    minimum: float,
    maximum: float,
    default: float = 0.0,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(minimum, parsed))
