from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from app.agents.editing.types import EditingPlanDecision, VideoContext
from app.core.config import get_settings


class EditingLLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class EditingLLM(Protocol):
    def plan_recipe(
        self,
        *,
        domain_context: str,
        project: dict[str, Any],
        selected_shortform: dict[str, Any],
        template: dict[str, Any],
        video_contexts: list[VideoContext],
        parent_recipe: dict[str, Any] | None,
        revision_action: str | None,
    ) -> EditingPlanDecision: ...

    def repair_recipe(
        self,
        *,
        domain_context: str,
        project: dict[str, Any],
        selected_shortform: dict[str, Any],
        template: dict[str, Any],
        video_contexts: list[VideoContext],
        decision: EditingPlanDecision,
        validation_errors: list[str],
        parent_recipe: dict[str, Any] | None,
        revision_action: str | None,
    ) -> EditingPlanDecision: ...


class OpenAIEditingLLM:
    """Editing-specific Responses API application with strict structured output."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.openai_api_key.strip()
        self.base_url = settings.openai_base_url.rstrip("/")
        self.model = settings.editing_openai_model.strip()
        self.timeout = settings.editing_request_timeout_seconds
        self.max_output_tokens = settings.editing_max_output_tokens

    def plan_recipe(
        self,
        *,
        domain_context: str,
        project: dict[str, Any],
        selected_shortform: dict[str, Any],
        template: dict[str, Any],
        video_contexts: list[VideoContext],
        parent_recipe: dict[str, Any] | None,
        revision_action: str | None,
    ) -> EditingPlanDecision:
        task = "Revise the parent EditRecipe" if revision_action else "Create an EditRecipe"
        payload = {
            "task": task,
            "project": project,
            "selected_shortform": selected_shortform,
            "editing_template": template,
            "video_contexts": _text_video_contexts(video_contexts),
            "parent_recipe": parent_recipe,
            "revision_action": revision_action,
            "renderer_capabilities": _renderer_capabilities(),
            "requirements": _requirements(),
        }
        return self._request(domain_context, payload, video_contexts, "editing_plan")

    def repair_recipe(
        self,
        *,
        domain_context: str,
        project: dict[str, Any],
        selected_shortform: dict[str, Any],
        template: dict[str, Any],
        video_contexts: list[VideoContext],
        decision: EditingPlanDecision,
        validation_errors: list[str],
        parent_recipe: dict[str, Any] | None,
        revision_action: str | None,
    ) -> EditingPlanDecision:
        payload = {
            "task": "Repair the EditRecipe so every deterministic validation error is fixed.",
            "project": project,
            "selected_shortform": selected_shortform,
            "editing_template": template,
            "video_contexts": _text_video_contexts(video_contexts),
            "invalid_decision": decision.model_dump(mode="json"),
            "validation_errors": validation_errors,
            "parent_recipe": parent_recipe,
            "revision_action": revision_action,
            "renderer_capabilities": _renderer_capabilities(),
            "requirements": _requirements(),
        }
        return self._request(domain_context, payload, video_contexts, "editing_plan_repair")

    def _request(
        self,
        instructions: str,
        user_payload: dict[str, Any],
        video_contexts: list[VideoContext],
        schema_name: str,
    ) -> EditingPlanDecision:
        if not self.api_key or not self.model:
            raise EditingLLMError(
                "OPENAI_API_KEY or EDITING_OPENAI_MODEL is not configured.", retryable=False
            )

        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": json.dumps(user_payload, ensure_ascii=False, default=str),
            }
        ]
        for context in video_contexts:
            for frame in context.keyframes:
                content.append(
                    {
                        "type": "input_text",
                        "text": f"video_id={context.video_id}, timestamp_ms={frame.timestamp_ms}",
                    }
                )
                content.append(
                    {"type": "input_image", "image_url": frame.image_url, "detail": "low"}
                )

        schema = _make_strict_schema(EditingPlanDecision.model_json_schema())
        request_payload = {
            "model": self.model,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
            "reasoning": {"effort": "low"},
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                )
        except httpx.TimeoutException as exc:
            raise EditingLLMError("Editing GPT request timed out.") from exc
        except httpx.HTTPError as exc:
            raise EditingLLMError("Editing GPT request failed.") from exc

        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise EditingLLMError(
                f"OpenAI Responses API returned HTTP {response.status_code}.",
                retryable=retryable,
            )
        try:
            data = response.json()
            output_text = _extract_output_text(data)
            parsed = json.loads(output_text)
            return EditingPlanDecision.model_validate(parsed)
        except (ValueError, TypeError) as exc:
            raise EditingLLMError("Editing GPT returned invalid structured output.") from exc


def _text_video_contexts(contexts: list[VideoContext]) -> list[dict[str, Any]]:
    return [
        {
            "video_id": item.video_id,
            "shooting_scene_order": item.shooting_scene_order,
            "duration_ms": item.duration_ms,
            "width": item.width,
            "height": item.height,
            "fps": item.fps,
            "keyframe_timestamps_ms": [frame.timestamp_ms for frame in item.keyframes],
        }
        for item in contexts
    ]


def _renderer_capabilities() -> dict[str, Any]:
    return {
        "source_type": "VIDEO_ONLY",
        "speed_range": [0.5, 2.0],
        "crop_modes": ["KEEP", "SUBJECT_CENTER", "CENTER_9_16"],
        "transitions": ["CUT", "HARD_CUT", "FLASH_WHITE"],
        "effects": ["PUNCH_ZOOM", "COLOR_TONE", "SMOOTH_ZOOM"],
        "caption_positions": ["BOTTOM", "MIDDLE", "TOP"],
        "font_weights": ["REGULAR", "SEMIBOLD", "BOLD"],
        "original_audio_policy": "REMOVE",
        "bgm_policy": "NONE",
    }


def _requirements() -> list[str]:
    return [
        "clip_order must be consecutive from 1 and timeline_start_ms must be gapless from 0.",
        "Preserve ascending shooting_scene_order and use only supplied video ids.",
        "Every source timestamp must be inside that video's duration.",
        "Caption times are absolute timeline milliseconds and must stay inside their clip.",
        "Use only renderer capabilities and the template editing_rules.",
        "Keep captions at most 40 characters each and at most 8 captions total.",
        "Publishing post_note must tell the user to add music in the platform.",
    ]


def _make_strict_schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_make_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _make_strict_schema(item)
        for key, item in value.items()
        if key not in {"default", "title"}
    }
    if result.get("type") == "object" or "properties" in result:
        properties = result.get("properties", {})
        result["required"] = list(properties)
        result["additionalProperties"] = False
    return result


def _extract_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    return text
    value = payload.get("output_text")
    return value if isinstance(value, str) else ""
