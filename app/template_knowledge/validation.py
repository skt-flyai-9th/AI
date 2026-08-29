from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.agents.editing.reals import RealsRegistry, get_reals_registry
from app.schemas.template_knowledge import (
    VideoEditingDBContent,
    ShootingGuideScene,
    TemplateType,
    TradeAreaDBContent,
)


class TemplateCandidateValidator:
    def __init__(self, registry: RealsRegistry | None = None) -> None:
        self.registry = registry or get_reals_registry()

    def validate(
        self,
        template_type: TemplateType | str,
        payload: dict[str, Any],
        *,
        is_initial_version: bool = False,
        base_payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if TemplateType(template_type) == TemplateType.TRADE_AREA:
            return self._validate_trade_area(payload)
        return self._validate_editing(
            payload,
            is_initial_version=is_initial_version,
            base_payload=base_payload,
        )

    @staticmethod
    def _validate_trade_area(payload: dict[str, Any]) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        try:
            content = TradeAreaDBContent.model_validate(payload)
        except ValidationError as exc:
            return _pydantic_errors(exc)

        dimension_keys = {item.key for item in content.analysis_dimensions}
        if len(dimension_keys) != len(content.analysis_dimensions):
            _add(
                errors,
                "DIMENSION_DUPLICATED",
                "analysis_dimensions",
                "Dimension keys must be unique.",
            )
        rule_ids = {item.rule_id for item in content.inference_rules}
        if len(rule_ids) != len(content.inference_rules):
            _add(errors, "RULE_DUPLICATED", "inference_rules", "Inference rule ids must be unique.")

        policy = content.policy
        if policy.aggregate_only is not True:
            _add(
                errors,
                "AGGREGATE_POLICY_REQUIRED",
                "policy.aggregate_only",
                "Trade-area analysis must use aggregate evidence only.",
            )
        if policy.no_individual_attribute_assertions is not True:
            _add(
                errors,
                "INDIVIDUAL_INFERENCE_FORBIDDEN",
                "policy.no_individual_attribute_assertions",
                "The trade-area DB must forbid individual attribute assertions.",
            )
        for index, rule in enumerate(content.inference_rules):
            serialized = str(rule.model_dump(mode="json")).lower()
            if "individual" in serialized and "forbid" not in serialized:
                _add(
                    errors,
                    "INDIVIDUAL_RULE_FORBIDDEN",
                    f"inference_rules[{index}]",
                    "Rules may describe aggregate segments, never individuals.",
                )
        return errors

    def _validate_editing(
        self,
        payload: dict[str, Any],
        *,
        is_initial_version: bool,
        base_payload: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        errors: list[dict[str, Any]] = []
        raw_metadata = payload.get("recommendation_metadata")
        if isinstance(raw_metadata, dict):
            if raw_metadata.get("requires_tts") is not False:
                _add(
                    errors,
                    "TTS_FORBIDDEN",
                    "recommendation_metadata.requires_tts",
                    "TTS is excluded from SARILS.",
                )
            if raw_metadata.get("requires_photo_input") is not False:
                _add(
                    errors,
                    "PHOTO_TIMELINE_FORBIDDEN",
                    "recommendation_metadata.requires_photo_input",
                    "Video-editing DB records must use recorded video only.",
                )
        try:
            content = VideoEditingDBContent.model_validate(payload)
        except ValidationError as exc:
            return errors + _pydantic_errors(exc)

        metadata = content.recommendation_metadata.model_dump(mode="json")
        if metadata.get("renderer_supported") is not True:
            _add(
                errors,
                "RENDERER_SUPPORT_REQUIRED",
                "recommendation_metadata.renderer_supported",
                "Only renderer-supported video-editing DB records can be activated.",
            )
        if metadata.get("source_type", "VIDEO_ONLY") != "VIDEO_ONLY":
            _add(
                errors,
                "SOURCE_TYPE_INVALID",
                "recommendation_metadata.source_type",
                "The only supported source type is VIDEO_ONLY.",
            )
        for key in (
            "supported_subject_types",
            "supported_objectives",
            "supported_filming_times",
            "supported_face_modes",
        ):
            if not isinstance(metadata.get(key), list) or not metadata[key]:
                _add(
                    errors,
                    "RECOMMENDATION_SCOPE_MISSING",
                    f"recommendation_metadata.{key}",
                    f"{key} must be a non-empty list.",
                )

        guide = content.shooting_guide
        raw_scenes = guide.scenes
        if not raw_scenes:
            _add(
                errors,
                "SHOOTING_GUIDE_EMPTY",
                "shooting_guide.scenes",
                "At least one video scene is required.",
            )
        else:
            scenes: list[ShootingGuideScene] = []
            for index, raw in enumerate(raw_scenes):
                try:
                    scenes.append(ShootingGuideScene.model_validate(raw))
                except ValidationError as exc:
                    for error in _pydantic_errors(exc):
                        error["path"] = f"shooting_guide.scenes[{index}].{error['path']}"
                        errors.append(error)
            if scenes:
                orders = [scene.scene_order for scene in scenes]
                if orders != list(range(1, len(scenes) + 1)):
                    _add(
                        errors,
                        "SCENE_ORDER_INVALID",
                        "shooting_guide.scenes",
                        "scene_order must be consecutive from 1.",
                    )

        tasks = guide.tasks
        if len(tasks) != len(raw_scenes):
            _add(
                errors,
                "SHOOTING_TASK_COUNT_MISMATCH",
                "shooting_guide.tasks",
                "Each shooting-guide scene must have exactly one matching task.",
            )
        if tasks:
            display_orders = [task.display_order for task in tasks]
            expected_orders = list(range(1, len(tasks) + 1))
            if display_orders != expected_orders:
                _add(
                    errors,
                    "SHOOTING_TASK_ORDER_INVALID",
                    "shooting_guide.tasks",
                    "task display_order must be consecutive from 1.",
                )
            scene_indexes = [task.scene_index for task in tasks]
            expected_indexes = list(range(len(tasks)))
            if scene_indexes != expected_indexes or any(
                index >= len(raw_scenes) for index in scene_indexes
            ):
                _add(
                    errors,
                    "SHOOTING_TASK_SCENE_INDEX_INVALID",
                    "shooting_guide.tasks",
                    "Tasks must map one-to-one to scenes using zero-based scene_index order.",
                )

        if base_payload is not None:
            base_guide = base_payload.get("shooting_guide") or {}
            base_task_count = len(base_guide.get("tasks") or [])
            base_scene_count = len(base_guide.get("scenes") or [])
            if base_task_count and len(tasks) < base_task_count:
                _add(
                    errors,
                    "SHOOTING_STRUCTURE_REGRESSION",
                    "shooting_guide.tasks",
                    (
                        f"Shooting task count dropped from {base_task_count} to {len(tasks)} "
                        "versus the current version. The shooting structure must never "
                        "shrink; regenerate the candidate with the full task set."
                    ),
                )
            if base_scene_count and len(raw_scenes) < base_scene_count:
                _add(
                    errors,
                    "SHOOTING_STRUCTURE_REGRESSION",
                    "shooting_guide.scenes",
                    (
                        f"Shooting scene count dropped from {base_scene_count} to {len(raw_scenes)} "
                        "versus the current version. The shooting structure must never "
                        "shrink; regenerate the candidate with the full scene set."
                    ),
                )

        rules = content.editing_rules
        render_profile_id = rules.render_profile_id
        render_profile = self.registry.render_profile(render_profile_id)
        if render_profile is None:
            _add(
                errors,
                "RENDER_PROFILE_UNKNOWN",
                "editing_rules.render_profile_id",
                f"Unknown REALS render profile: {render_profile_id}.",
            )
        assembly_profile_id = rules.assembly_profile_id
        if self.registry.render_profile(assembly_profile_id) is None:
            _add(
                errors,
                "ASSEMBLY_PROFILE_UNKNOWN",
                "editing_rules.assembly_profile_id",
                f"Unknown REALS assembly profile: {assembly_profile_id}.",
            )
        safe_area_profile_id = rules.safe_area_profile_id
        if not self.registry.has_safe_area_profile(safe_area_profile_id):
            _add(
                errors,
                "SAFE_AREA_PROFILE_UNKNOWN",
                "editing_rules.safe_area_profile_id",
                f"Unknown REALS safe-area profile: {safe_area_profile_id}.",
            )
        if rules.audio_policy != "SILENT_V1":
            _add(
                errors,
                "AUDIO_POLICY_INVALID",
                "editing_rules.audio_policy",
                "Video-editing DB records must remove source audio and leave platform music to publishing.",
            )
        allowed_effects = rules.allowed_effect_ids
        if not isinstance(allowed_effects, list) or not set(allowed_effects).issubset(
            self.registry.creative_effect_ids
        ):
            _add(
                errors,
                "EFFECT_UNSUPPORTED",
                "editing_rules.allowed_effect_ids",
                "Video-editing DB record contains an effect outside the REALS registry.",
            )
        allowed_transitions = rules.allowed_transition_ids
        renderer_transitions = self.registry.transition_ids | {"CUT"}
        if not isinstance(allowed_transitions, list) or not set(allowed_transitions).issubset(
            renderer_transitions
        ):
            _add(
                errors,
                "TRANSITION_UNSUPPORTED",
                "editing_rules.allowed_transition_ids",
                "Video-editing DB record contains a transition outside the REALS registry.",
            )
        min_cut = rules.min_cut_duration_ms
        registry_min = int(self.registry.edit_policies.get("min_cut_duration_ms", 300))
        if not isinstance(min_cut, int) or min_cut < registry_min:
            _add(
                errors,
                "MIN_CUT_TOO_SHORT",
                "editing_rules.min_cut_duration_ms",
                f"min_cut_duration_ms must be at least {registry_min}.",
            )
        max_duration = rules.max_duration_sec
        if (
            not isinstance(max_duration, (int, float))
            or max_duration <= 0
            or (
                render_profile is not None
                and max_duration > float(render_profile["max_duration_sec"])
            )
        ):
            _add(
                errors,
                "MAX_DURATION_INVALID",
                "editing_rules.max_duration_sec",
                "max_duration_sec must fit the selected REALS render profile.",
            )
        if not is_initial_version and not content.trend_ids:
            _add(
                errors,
                "TREND_EVIDENCE_REQUIRED",
                "trend_ids",
                "A video-editing DB update requires trendcluster evidence.",
            )
        return errors


def _pydantic_errors(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {
            "code": "SCHEMA_INVALID",
            "path": ".".join(str(item) for item in error["loc"]),
            "message": error["msg"],
        }
        for error in exc.errors()
    ]


def _add(errors: list[dict[str, Any]], code: str, path: str, message: str) -> None:
    errors.append({"code": code, "path": path, "message": message})
