from __future__ import annotations

from typing import Any

from app.agents.editing.reals import RealsRegistry, get_reals_registry
from app.agents.editing.types import ValidationIssue, VideoContext
from app.core.config import Settings, get_settings
from app.schemas.editing import EditRecipe, SelectedShortform


class EditRecipeValidator:
    """Domain preflight backed by the REALS engine's registry bundle.

    This runs before the network boundary so repairable LLM mistakes can be
    corrected cheaply. The REALS engine remains the final authority and runs
    its native validator again after remote assets have been assembled.
    """

    def __init__(
        self,
        registry: RealsRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry or get_reals_registry()
        self.settings = settings or get_settings()

    def validate(
        self,
        recipe: EditRecipe,
        *,
        selected_shortform: SelectedShortform,
        video_editing_db: dict[str, Any],
        video_contexts: list[VideoContext],
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []

        def add(
            code: str,
            path: str,
            message: str,
            *,
            source: str = "DOMAIN",
            repairable: bool = True,
        ) -> None:
            issues.append(
                ValidationIssue(
                    code=code,
                    path=path,
                    message=message,
                    source=source,
                    repairable=repairable,
                )
            )

        if recipe.editing_template_id != selected_shortform.editing_template_id:
            add(
                "EDITING_TEMPLATE_ID_MISMATCH",
                "editing_template_id",
                "editing_template_id must match selected_shortform.",
            )
        if recipe.editing_template_version != selected_shortform.editing_template_version:
            add(
                "EDITING_TEMPLATE_VERSION_MISMATCH",
                "editing_template_version",
                "editing_template_version must match selected_shortform.",
            )

        rules = video_editing_db.get("editing_rules") or {}
        renderer_effects = self.registry.creative_effect_ids
        configured_effects = rules.get("allowed_effect_ids")
        allowed_effects = (
            set(configured_effects)
            if isinstance(configured_effects, list)
            else set(renderer_effects)
        )
        allowed_effects &= renderer_effects
        allowed_effects -= self.settings.editing_disabled_effect_ids_set

        renderer_transitions = self.registry.transition_ids | {"CUT"}
        configured_transitions = rules.get("allowed_transition_ids")
        allowed_transitions = (
            set(configured_transitions)
            if isinstance(configured_transitions, list)
            else set(renderer_transitions)
        )
        allowed_transitions &= renderer_transitions
        allowed_transitions.add(None)

        policies = self.registry.edit_policies
        registry_min_cut_ms = int(policies.get("min_cut_duration_ms", 300))
        min_cut_ms = max(registry_min_cut_ms, int(rules.get("min_cut_duration_ms") or 0))
        render_profile_id = str(rules.get("render_profile_id") or "INSTAGRAM_REELS_V1")
        render_profile = self.registry.render_profile(render_profile_id)
        if render_profile is None:
            add(
                "RENDER_PROFILE_UNKNOWN",
                "video_editing_db.editing_rules.render_profile_id",
                f"Unknown REALS render profile: {render_profile_id}.",
                source="REALS_REGISTRY",
                repairable=False,
            )
            profile_max_duration_ms = 60_000
        else:
            profile_max_duration_ms = int(float(render_profile["max_duration_sec"]) * 1000)
        database_max_duration_ms = int(float(rules.get("max_duration_sec") or 90) * 1000)
        runtime_max_duration_ms = self.settings.editing_max_output_duration_seconds * 1000
        max_duration_ms = min(
            database_max_duration_ms,
            profile_max_duration_ms,
            runtime_max_duration_ms,
        )

        safe_area_profile_id = str(
            rules.get("safe_area_profile_id") or "INSTAGRAM_REELS_2026_V1"
        )
        if not self.registry.has_safe_area_profile(safe_area_profile_id):
            add(
                "SAFE_AREA_PROFILE_UNKNOWN",
                "video_editing_db.editing_rules.safe_area_profile_id",
                f"Unknown REALS safe-area profile: {safe_area_profile_id}.",
                source="REALS_REGISTRY",
                repairable=False,
            )
        if self.registry.audio_mix_policy("SILENT_V1") is None:
            add(
                "AUDIO_POLICY_MISSING",
                "video_editing_db.editing_rules",
                "REALS audio policy SILENT_V1 is missing.",
                source="REALS_REGISTRY",
                repairable=False,
            )
        assembly_profile_id = str(
            rules.get("assembly_profile_id") or "INTERMEDIATE_VERTICAL_V1"
        )
        if len(recipe.timeline) > 1 and self.registry.render_profile(assembly_profile_id) is None:
            add(
                "ASSEMBLY_PROFILE_UNKNOWN",
                "video_editing_db.editing_rules.assembly_profile_id",
                f"Unknown REALS assembly profile: {assembly_profile_id}.",
                source="REALS_REGISTRY",
                repairable=False,
            )

        contexts = {context.video_id: context for context in video_contexts}
        order_by_video = {
            context.video_id: context.shooting_scene_order for context in video_contexts
        }
        clip_orders = [clip.clip_order for clip in recipe.timeline]
        expected_orders = list(range(1, len(recipe.timeline) + 1))
        if clip_orders != expected_orders:
            add(
                "CLIP_ORDER_NON_CONSECUTIVE",
                "timeline",
                f"clip_order must be consecutive; expected {expected_orders}.",
            )

        expected_start = 0.0
        scene_orders: list[int] = []
        caption_count = 1  # CTA is rendered as a caption overlay.
        for index, clip in enumerate(recipe.timeline):
            path = f"timeline[{index}]"
            context = contexts.get(clip.video_id)
            if context is None:
                add(
                    "VIDEO_UNKNOWN",
                    f"{path}.video_id",
                    f"Unknown video_id: {clip.video_id}.",
                )
                continue
            scene_orders.append(order_by_video[clip.video_id])
            if clip.source_start_ms >= clip.source_end_ms:
                add(
                    "SOURCE_RANGE_INVALID",
                    path,
                    "source_start_ms must be before source_end_ms.",
                )
                continue
            if clip.source_end_ms > context.duration_ms:
                add(
                    "SOURCE_RANGE_OUT_OF_BOUNDS",
                    f"{path}.source_end_ms",
                    f"source_end_ms exceeds {clip.video_id} duration ({context.duration_ms}ms).",
                )
            source_duration = clip.source_end_ms - clip.source_start_ms
            output_duration = source_duration / clip.speed
            if output_duration < min_cut_ms:
                add(
                    "CUT_TOO_SHORT",
                    path,
                    f"Output duration must be at least {min_cut_ms}ms.",
                    source="REALS_REGISTRY",
                )
            if abs(clip.timeline_start_ms - expected_start) > 1:
                add(
                    "TIMELINE_NOT_GAPLESS",
                    f"{path}.timeline_start_ms",
                    f"timeline_start_ms must be {round(expected_start)}.",
                )
            clip_end = clip.timeline_start_ms + output_duration
            expected_start += output_duration

            self._validate_transition(
                clip.transition_in,
                f"{path}.transition_in",
                allowed_transitions,
                add,
            )
            self._validate_transition(
                clip.transition_out,
                f"{path}.transition_out",
                allowed_transitions,
                add,
            )
            if index > 0:
                previous = recipe.timeline[index - 1].transition_out
                if (
                    clip.transition_in is not None
                    and previous is not None
                    and _normalize_transition(clip.transition_in)
                    != _normalize_transition(previous)
                ):
                    add(
                        "TRANSITION_CONFLICT",
                        f"{path}.transition_in",
                        "transition_in conflicts with the previous clip's transition_out.",
                    )

            color_tone_count = 0
            for effect_index, effect in enumerate(clip.effects):
                effect_path = f"{path}.effects[{effect_index}]"
                if effect.effect_id not in allowed_effects:
                    add(
                        "EFFECT_UNSUPPORTED",
                        f"{effect_path}.effect_id",
                        f"Unsupported effect_id: {effect.effect_id}.",
                        source="REALS_REGISTRY",
                    )
                    continue
                if effect.effect_id == "COLOR_TONE":
                    color_tone_count += 1
                self._validate_effect_params(
                    effect.effect_id,
                    effect.params.model_dump(exclude_none=True),
                    effect_path,
                    add,
                )
            if color_tone_count > 1:
                add(
                    "COLOR_TONE_DUPLICATED",
                    f"{path}.effects",
                    "At most one COLOR_TONE effect is allowed per clip.",
                )

            if clip.caption is not None:
                caption_count += 1
                caption = clip.caption
                caption_path = f"{path}.caption"
                if caption.start_ms >= caption.end_ms:
                    add(
                        "CAPTION_RANGE_INVALID",
                        caption_path,
                        "Caption start_ms must be before end_ms.",
                    )
                if caption.start_ms < clip.timeline_start_ms or caption.end_ms > clip_end + 1:
                    add(
                        "CAPTION_OUTSIDE_CLIP",
                        caption_path,
                        "Caption must stay inside its clip's output timeline.",
                    )
                max_chars = int(policies.get("max_caption_chars", 40))
                if len(caption.text) > max_chars:
                    add(
                        "CAPTION_TOO_LONG",
                        f"{caption_path}.text",
                        f"Caption exceeds the REALS limit of {max_chars} characters.",
                        source="REALS_REGISTRY",
                    )
                if caption.style_id not in self.registry.caption_style_ids():
                    add(
                        "CAPTION_STYLE_UNKNOWN",
                        f"{caption_path}.style_id",
                        f"Unknown REALS caption style: {caption.style_id}.",
                        source="REALS_REGISTRY",
                    )
                if caption.scale != 1.0:
                    add(
                        "CAPTION_SCALE_UNSUPPORTED",
                        f"{caption_path}.scale",
                        "REALS does not accept arbitrary caption scale; use 1.0 and an approved style.",
                        source="REALS_REGISTRY",
                    )

        if scene_orders != sorted(scene_orders):
            add(
                "SHOOTING_ORDER_CHANGED",
                "timeline",
                "Timeline must preserve shooting_scene_order.",
            )
        max_captions = int(policies.get("max_captions_per_video", 8))
        if caption_count > max_captions:
            add(
                "CAPTION_COUNT_EXCEEDED",
                "timeline",
                f"Captions including CTA exceed the REALS limit of {max_captions}.",
                source="REALS_REGISTRY",
            )
        max_chars = int(policies.get("max_caption_chars", 40))
        if len(recipe.cta.text) > max_chars:
            add(
                "CTA_TOO_LONG",
                "cta.text",
                f"CTA exceeds the REALS limit of {max_chars} characters.",
                source="REALS_REGISTRY",
            )
        if "CTA_BOX" not in self.registry.caption_style_ids():
            add(
                "CTA_STYLE_MISSING",
                "cta",
                "REALS caption style CTA_BOX is missing.",
                source="REALS_REGISTRY",
                repairable=False,
            )
        if expected_start > max_duration_ms:
            add(
                "OUTPUT_TOO_LONG",
                "timeline",
                f"Output duration exceeds the effective limit of {max_duration_ms}ms.",
                source="REALS_REGISTRY",
            )
        return issues

    @staticmethod
    def _validate_transition(
        value: str | None,
        path: str,
        allowed: set[str | None],
        add,
    ) -> None:
        if value not in allowed:
            add(
                "TRANSITION_UNSUPPORTED",
                path,
                f"Unsupported transition: {value}.",
                source="REALS_REGISTRY",
            )

    def _validate_effect_params(
        self,
        effect_id: str,
        params: dict[str, Any],
        path: str,
        add,
    ) -> None:
        effect = self.registry.effect_rules(effect_id)
        if effect is None:
            return
        allowed_params = effect.get("allowed_params", {})
        for name in params:
            if name not in allowed_params:
                add(
                    "EFFECT_PARAM_UNKNOWN",
                    f"{path}.params.{name}",
                    f"{effect_id} does not support parameter {name}.",
                    source="REALS_REGISTRY",
                )
        for name, rule in allowed_params.items():
            value = params.get(name)
            if value is None:
                add(
                    "EFFECT_PARAM_MISSING",
                    f"{path}.params.{name}",
                    f"{effect_id}.{name} is required.",
                    source="REALS_REGISTRY",
                )
                continue
            if "enum" in rule and value not in rule["enum"]:
                add(
                    "EFFECT_PARAM_INVALID",
                    f"{path}.params.{name}",
                    f"{effect_id}.{name} must be one of {rule['enum']}.",
                    source="REALS_REGISTRY",
                )
            if "min" in rule and not float(rule["min"]) <= float(value) <= float(rule["max"]):
                add(
                    "EFFECT_PARAM_OUT_OF_RANGE",
                    f"{path}.params.{name}",
                    f"{effect_id}.{name} must be between {rule['min']} and {rule['max']}.",
                    source="REALS_REGISTRY",
                )


def _normalize_transition(value: str) -> str:
    return "HARD_CUT" if value == "CUT" else value
