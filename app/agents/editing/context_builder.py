from __future__ import annotations

from typing import Any

from app.agents.editing.types import VideoContext


def build_editing_context(
    *,
    project: dict[str, Any],
    selected_shortform: dict[str, Any],
    video_editing_db: dict[str, Any],
    video_contexts: list[VideoContext],
    prepared_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Combine existing request, guide, and frame evidence for recipe planning.

    The backend already supplies the project and ordered footage. The AI service
    owns the selected guide/rules and the observed frame evidence. This builder
    joins those sources without changing the external editing-run contract.
    """

    shooting_guide = _mapping(video_editing_db.get("shooting_guide"))
    scenes = _dict_items(shooting_guide.get("scenes"))
    tasks = _dict_items(shooting_guide.get("tasks"))
    source_preparation = _mapping(prepared_analysis.get("source_preparation"))
    produced_context = _mapping(prepared_analysis.get("produced_frame_context"))
    observations = _dict_items(produced_context.get("observations"))
    shoot_mode = _normalized_shoot_mode(source_preparation)

    return {
        "context_version": "editing-context-v1",
        "project_context": {
            "project_id": project.get("project_id"),
            "store_id": project.get("store_id"),
            "promotion_subject": _mapping(project.get("promotion_subject")),
            "promotion_objective": project.get("promotion_objective"),
            "face_exposure": project.get("face_exposure"),
        },
        "template_context": {
            "editing_template_id": selected_shortform.get("editing_template_id"),
            "editing_template_version": selected_shortform.get("editing_template_version"),
            "name": video_editing_db.get("name"),
            "recommendation_title": video_editing_db.get("recommendation_title"),
            "recommendation_concept": video_editing_db.get("recommendation_concept"),
            "shoot_mode": shoot_mode,
            "editing_rules": _mapping(video_editing_db.get("editing_rules")),
            "reference_segment_count": len(
                _dict_items(
                    _mapping(video_editing_db.get("reference_evidence")).get("reference_segments")
                )
            ),
        },
        "source_scenes": [
            _source_scene_context(
                context=context,
                scenes=scenes,
                tasks=tasks,
                observations=observations,
                source_preparation=source_preparation,
                shoot_mode=shoot_mode,
            )
            for context in sorted(video_contexts, key=lambda item: item.shooting_scene_order)
        ],
    }


def _source_scene_context(
    *,
    context: VideoContext,
    scenes: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    source_preparation: dict[str, Any],
    shoot_mode: str,
) -> dict[str, Any]:
    if shoot_mode == "ONE_TAKE":
        expected_scenes = scenes
        expected_tasks = tasks
    else:
        scene_index = context.shooting_scene_order - 1
        expected_scenes = [
            scene
            for scene in scenes
            if _as_int(scene.get("scene_order")) == context.shooting_scene_order
        ]
        expected_tasks = [task for task in tasks if _as_int(task.get("scene_index")) == scene_index]

    video_observations = [item for item in observations if item.get("video_id") == context.video_id]
    return {
        "video_id": context.video_id,
        "shooting_scene_order": context.shooting_scene_order,
        "duration_ms": context.duration_ms,
        "width": context.width,
        "height": context.height,
        "fps": context.fps,
        "expected_scenes": [_expected_scene(item) for item in expected_scenes],
        "expected_tasks": [_expected_task(item) for item in expected_tasks],
        "selected_source": _selected_source(source_preparation, context),
        "observed_context": {
            "semantic_events": _unique_values(video_observations, "semantic_event"),
            "subjects": _unique_values(video_observations, "subject"),
            "actions": _unique_values(video_observations, "action"),
            "compositions": _unique_values(video_observations, "composition"),
            "camera_motions": _unique_values(video_observations, "camera_motion"),
            "quality_flags": _unique_list_values(video_observations, "quality_flags"),
            "frame_observation_count": len(video_observations),
        },
    }


def _expected_scene(scene: dict[str, Any]) -> dict[str, Any]:
    return {
        "scene_order": scene.get("scene_order"),
        "scene_role": scene.get("scene_role") or scene.get("role"),
        "scene_description": scene.get("scene_description"),
        "shot_type": scene.get("shot_type"),
        "target_duration_sec": scene.get("target_duration_sec"),
    }


def _expected_task(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "display_order": task.get("display_order"),
        "task_title": task.get("task_title"),
        "instructions": _string_items(_mapping(task.get("guide")).get("instructions")),
    }


def _selected_source(source_preparation: dict[str, Any], context: VideoContext) -> dict[str, Any]:
    if source_preparation.get("mode") == "MULTI_CUT":
        cut = next(
            (
                item
                for item in _dict_items(source_preparation.get("cuts"))
                if item.get("video_id") == context.video_id
            ),
            {},
        )
        return {
            "trim_in_ms": cut.get("trim_in_ms"),
            "trim_out_ms": cut.get("trim_out_ms"),
            "mapped_reference_segment_id": cut.get("mapped_reference_segment_id"),
            "decision_reason": cut.get("decision_reason"),
        }
    return {
        "trim_in_ms": source_preparation.get("trim_in_ms", 0),
        "trim_out_ms": source_preparation.get("trim_out_ms", context.duration_ms),
        "mapped_reference_segment_id": None,
        "decision_reason": "ONE_TAKE_PASSTHROUGH",
    }


def _normalized_shoot_mode(source_preparation: dict[str, Any]) -> str:
    return "MULTI_CUT" if source_preparation.get("mode") == "MULTI_CUT" else "ONE_TAKE"


def _unique_values(observations: list[dict[str, Any]], key: str, *, limit: int = 20) -> list[str]:
    values: list[str] = []
    for item in observations:
        value = str(item.get(key) or "").strip()
        if not value or value.upper() in {"NONE", "UNKNOWN"} or value in values:
            continue
        values.append(value[:200])
        if len(values) >= limit:
            break
    return values


def _unique_list_values(
    observations: list[dict[str, Any]], key: str, *, limit: int = 20
) -> list[str]:
    values: list[str] = []
    for item in observations:
        for value in _string_items(item.get(key)):
            if value and value not in values:
                values.append(value[:200])
                if len(values) >= limit:
                    return values
    return values


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _string_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
