from __future__ import annotations

import unicodedata
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
        project: dict[str, Any] | None = None,
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
            if isinstance(configured_effects, list) and configured_effects
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

        safe_area_profile_id = str(rules.get("safe_area_profile_id") or "INSTAGRAM_REELS_2026_V1")
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
        assembly_profile_id = str(rules.get("assembly_profile_id") or "INTERMEDIATE_VERTICAL_V1")
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
        typewriter_count = 0
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
                    and _normalize_transition(clip.transition_in) != _normalize_transition(previous)
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
                params = effect.params.model_dump(exclude_none=True)
                if (
                    params.get("start_ms") is not None
                    and params.get("end_ms") is not None
                    and (
                        int(params["start_ms"]) < 0 or int(params["end_ms"]) > int(output_duration)
                    )
                ):
                    add(
                        "EFFECT_WINDOW_OUTSIDE_CLIP",
                        f"{effect_path}.params",
                        (
                            f"{effect.effect_id} effect window must be inside the clip-relative "
                            f"range 0..{int(output_duration)}ms."
                        ),
                        source="REALS_REGISTRY",
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
                if caption.motion_id not in self.registry.caption_motion_ids():
                    add(
                        "CAPTION_MOTION_UNKNOWN",
                        f"{caption_path}.motion_id",
                        f"Unknown REALS caption motion: {caption.motion_id}.",
                        source="REALS_REGISTRY",
                    )
                caption_duration_ms = caption.end_ms - caption.start_ms
                if caption.motion_id == "TYPEWRITER":
                    typewriter_count += 1
                    unit_count = _typewriter_unit_count(caption.text)
                    if unit_count > 18:
                        add(
                            "TYPEWRITER_CAPTION_TOO_LONG",
                            f"{caption_path}.text",
                            "TYPEWRITER captions support at most 18 non-space characters.",
                            source="REALS_REGISTRY",
                        )
                    required_ms = max(0, unit_count - 1) * 80 + 600
                    if caption_duration_ms < required_ms:
                        add(
                            "TYPEWRITER_CAPTION_TOO_SHORT",
                            caption_path,
                            "TYPEWRITER caption needs 80ms per character and at least "
                            f"600ms hold time ({required_ms}ms required).",
                            source="REALS_REGISTRY",
                        )
                else:
                    readability_target_ms = max(
                        900,
                        _typewriter_unit_count(caption.text) * 60 + 400,
                    )
                    # A caption cannot outlive the clip that owns it. For very
                    # short cuts, displaying the caption for the entire cut is
                    # the longest valid and therefore sufficient duration.
                    readable_ms = min(readability_target_ms, max(1, int(output_duration)))
                    if caption_duration_ms < readable_ms:
                        add(
                            "CAPTION_DURATION_TOO_SHORT",
                            caption_path,
                            "Caption must stay on screen long enough to read "
                            f"({readable_ms}ms required for this text length).",
                            source="REALS_REGISTRY",
                        )
                requested_min_ms = _requested_min_caption_display_ms(project)
                if requested_min_ms is not None and caption_duration_ms < requested_min_ms:
                    add(
                        "PROJECT_CAPTION_DURATION_TOO_SHORT",
                        caption_path,
                        "The project requested captions stay visible for at least "
                        f"{requested_min_ms}ms; this caption is {caption_duration_ms}ms.",
                    )
                requested_position = _requested_caption_position(project)
                if requested_position is not None and caption.position != requested_position:
                    add(
                        "PROJECT_CAPTION_POSITION_MISMATCH",
                        f"{caption_path}.position",
                        f"The project requested captions positioned at {requested_position}; "
                        f"received {caption.position}.",
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
        if typewriter_count > 2:
            add(
                "TYPEWRITER_COUNT_EXCEEDED",
                "timeline",
                "Use TYPEWRITER on at most 2 captions per video.",
                source="REALS_REGISTRY",
            )
        if _is_promotional_project(project):
            required_phrases = _required_verbatim_caption_phrases(project)
            regular_caption_count = caption_count - 1
            required_caption_count = min(3, len(recipe.timeline))
            if regular_caption_count < required_caption_count:
                add(
                    "PROMOTIONAL_CAPTIONS_MISSING",
                    "timeline",
                    "Promotional video requires at least "
                    f"{required_caption_count} regular in-video captions; "
                    f"received {regular_caption_count}. The final CTA does not count.",
                )
            first_caption = recipe.timeline[0].caption if recipe.timeline else None
            if first_caption is None or first_caption.style_id != "HOOK":
                add(
                    "PROMOTIONAL_HOOK_MISSING",
                    "timeline[0].caption",
                    "The first promotional clip requires a HOOK caption grounded in the project.",
                )
            elif not _caption_contains_promotion_subject(first_caption.text, project):
                add(
                    "PROMOTIONAL_HOOK_NOT_PERSONALIZED",
                    "timeline[0].caption.text",
                    "The first promotional HOOK must name the verified promotion subject.",
                )
            for index, clip in enumerate(recipe.timeline):
                if clip.caption is None:
                    continue
                if _is_stage_direction_caption(clip.caption.text, required_phrases):
                    add(
                        "PROMOTIONAL_CAPTION_IS_STAGE_DIRECTION",
                        f"timeline[{index}].caption.text",
                        "Promotional captions must be audience-facing copy, not filming or editing directions.",
                    )
            # A single-clip recipe has exactly one caption slot and the HOOK
            # rule above already claims it, so the reveal caption can only be
            # required once a second clip exists.
            if len(recipe.timeline) >= 2 and not any(
                clip.caption is not None and clip.caption.style_id == "CAPTION_EMPHASIS"
                for clip in recipe.timeline
            ):
                add(
                    "PROMOTIONAL_REVEAL_CAPTION_MISSING",
                    "timeline",
                    "Promotional video requires a CAPTION_EMPHASIS overlay on an item or reveal moment.",
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
        rendered_copy = "\n".join(
            [
                *(clip.caption.text for clip in recipe.timeline if clip.caption is not None),
                recipe.cta.text,
            ]
        )
        normalized_rendered_copy = unicodedata.normalize("NFC", rendered_copy)
        for phrase in _required_verbatim_caption_phrases(project):
            if unicodedata.normalize("NFC", phrase) not in normalized_rendered_copy:
                add(
                    "PROJECT_CAPTION_PHRASE_MISSING",
                    "timeline",
                    f"Project-scoped caption phrase must be preserved exactly: {phrase!r}.",
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


def _is_promotional_project(project: dict[str, Any] | None) -> bool:
    if not isinstance(project, dict):
        return False
    objective = str(project.get("promotion_objective") or "").strip()
    subject = project.get("promotion_subject")
    return bool(objective and isinstance(subject, dict) and subject)


def _copy_directives(project: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(project, dict):
        return {}
    shortform_context = project.get("shortform_context")
    if not isinstance(shortform_context, dict):
        return {}
    directives = shortform_context.get("copy_directives")
    return directives if isinstance(directives, dict) else {}


def _required_verbatim_caption_phrases(project: dict[str, Any] | None) -> list[str]:
    phrases = _copy_directives(project).get("verbatim_caption_phrases")
    if not isinstance(phrases, list):
        return []
    return [str(phrase).strip() for phrase in phrases if str(phrase).strip()]


def _requested_caption_position(project: dict[str, Any] | None) -> str | None:
    value = _copy_directives(project).get("caption_position_request")
    return value if value in {"TOP", "MIDDLE", "BOTTOM"} else None


def _requested_min_caption_display_ms(project: dict[str, Any] | None) -> int | None:
    value = _copy_directives(project).get("requested_min_caption_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _caption_contains_promotion_subject(caption: str, project: dict[str, Any] | None) -> bool:
    if not isinstance(project, dict):
        return False
    subject = project.get("promotion_subject")
    if not isinstance(subject, dict):
        return False
    normalized_caption = _normalize_copy(caption)
    terms: list[str] = []
    for key in ("name", "menu_name", "title", "description"):
        value = subject.get(key)
        if isinstance(value, str) and value.strip():
            terms.append(value)
    elements = subject.get("elements")
    if isinstance(elements, list):
        terms.extend(str(value) for value in elements if str(value).strip())
    return any(_normalize_copy(term) in normalized_caption for term in terms)


def _is_stage_direction_caption(text: str, required_phrases: list[str]) -> bool:
    normalized_text = _normalize_copy(text)
    if any(_normalize_copy(phrase) in normalized_text for phrase in required_phrases):
        return False
    markers = (
        "클로즈업",
        "전환",
        "세팅",
        "의상변경",
        "다음장면",
        "보이기",
        "손바닥",
        "양손을펼쳐",
        "손을펼치면",
    )
    return any(marker in normalized_text for marker in markers)


def _normalize_copy(value: str) -> str:
    return "".join(unicodedata.normalize("NFC", value).casefold().split())


def _typewriter_unit_count(value: str) -> int:
    normalized = unicodedata.normalize("NFC", value)
    return sum(1 for character in normalized if not character.isspace())
