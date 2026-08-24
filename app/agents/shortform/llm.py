from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from app.agents.shortform.types import (
    ShortformTurnDecision,
    TemplateCandidate,
    TemplateSelection,
)
from app.core.config import get_settings


class ShortformLLMError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retryable: bool = True):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ShortformLLM(Protocol):
    def decide_turn(
        self,
        *,
        domain_context: str,
        store_context: dict[str, Any],
        project_state: dict[str, Any],
        conversation: list[dict[str, str]],
        user_input: dict[str, Any],
        photo_urls: list[str],
    ) -> ShortformTurnDecision: ...

    def select_template(
        self,
        *,
        domain_context: str,
        store_context: dict[str, Any],
        project_state: dict[str, Any],
        conversation: list[dict[str, str]],
        candidates: list[TemplateCandidate],
    ) -> TemplateSelection: ...


class OpenAIShortformLLM:
    """Shortform-specific GPT application using the Responses API.

    This application owns a separate domain prompt and schemas even when other
    GPT applications share the same underlying model family.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.openai_api_key.strip()
        self.base_url = settings.openai_base_url.rstrip("/")
        self.model = settings.shortform_openai_model.strip()
        self.timeout = settings.shortform_request_timeout_seconds
        self.max_output_tokens = settings.shortform_max_output_tokens

    def decide_turn(
        self,
        *,
        domain_context: str,
        store_context: dict[str, Any],
        project_state: dict[str, Any],
        conversation: list[dict[str, str]],
        user_input: dict[str, Any],
        photo_urls: list[str],
    ) -> ShortformTurnDecision:
        prompt = {
            "task": "Process exactly one Shortform Agent conversation turn.",
            "store_context": store_context,
            "project_state": project_state,
            "recent_conversation": conversation[-20:],
            "current_user_input": user_input,
            "requirements": [
                "Return exactly one allowed action.",
                "Every field required by the structured schema must be returned; use null or empty lists when there is no update.",
                "Do not ask for information already present in project_state or store_context.",
                "Do not invent factual store/menu/event information.",
                "When all four required fields are known, use CONFIRM rather than recommending immediately.",
                "RECOMMEND is only valid after the brief has already been confirmed.",
                "Option ids must be short semantic stable ids such as MENU, sales, within_10m, not_allowed, or a real menu_id.",
            ],
        }
        return ShortformTurnDecision.model_validate(
            self._request_json(
                instructions=domain_context,
                user_payload=prompt,
                schema_name="shortform_turn_decision",
                schema=ShortformTurnDecision.model_json_schema(),
                photo_urls=photo_urls,
            )
        )

    def select_template(
        self,
        *,
        domain_context: str,
        store_context: dict[str, Any],
        project_state: dict[str, Any],
        conversation: list[dict[str, str]],
        candidates: list[TemplateCandidate],
    ) -> TemplateSelection:
        if not candidates:
            raise ShortformLLMError("No candidate templates were provided.", retryable=False)

        allowed_keys = [candidate.candidate_key for candidate in candidates]
        selection_schema = TemplateSelection.model_json_schema()
        selection_schema["properties"]["candidate_key"] = {
            "type": "string",
            "enum": allowed_keys,
        }

        prompt = {
            "task": (
                "Choose exactly one candidate ACTIVE editing template for the current store/project. "
                "Do not invent a new shortform format."
            ),
            "store_context": store_context,
            "project_state": project_state,
            "recent_conversation": conversation[-20:],
            "candidate_templates": [candidate.model_dump(mode="json") for candidate in candidates],
            "requirements": [
                "Choose only candidate_key from candidate_templates.",
                "Use the whole user conversation and Store Context, not a fixed weighted ranking.",
                "project_title/title/concept may adapt wording to this store but must preserve the selected template concept.",
                "Keep title and concept concise for UI display.",
                "internal_reason is for logs only and must explain the contextual selection briefly.",
            ],
        }

        return TemplateSelection.model_validate(
            self._request_json(
                instructions=domain_context,
                user_payload=prompt,
                schema_name="shortform_template_selection",
                schema=selection_schema,
                photo_urls=[],
            )
        )

    def _request_json(
        self,
        *,
        instructions: str,
        user_payload: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
        photo_urls: list[str],
    ) -> dict[str, Any]:
        if not self.api_key or not self.model:
            raise ShortformLLMError(
                "OPENAI_API_KEY or SHORTFORM_OPENAI_MODEL is not configured.",
                status_code=503,
                retryable=False,
            )

        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": json.dumps(user_payload, ensure_ascii=False, default=str),
            }
        ]
        for url in photo_urls:
            if url:
                content.append({"type": "input_image", "image_url": url, "detail": "low"})

        payload = {
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
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise ShortformLLMError("Shortform GPT request timed out.", status_code=503) from exc
        except httpx.HTTPError as exc:
            raise ShortformLLMError("Shortform GPT request failed.", status_code=503) from exc

        if response.status_code >= 400:
            if response.status_code == 429:
                raise ShortformLLMError(
                    "OpenAI Responses API rate limit was reached.",
                    status_code=429,
                    retryable=True,
                )
            retryable = response.status_code >= 500 or response.status_code in {408, 409}
            # Upstream auth/config/schema failures are server dependency failures,
            # not backend X-Internal-API-Key failures, so expose them as 503.
            raise ShortformLLMError(
                f"OpenAI Responses API returned HTTP {response.status_code}.",
                status_code=503,
                retryable=retryable,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ShortformLLMError("OpenAI response was not valid JSON.", status_code=503) from exc
        text = _extract_output_text(data)
        if not text:
            raise ShortformLLMError("OpenAI response contained no structured text output.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ShortformLLMError("OpenAI structured output was not valid JSON.") from exc
        if not isinstance(parsed, dict):
            raise ShortformLLMError("OpenAI structured output must be a JSON object.")
        return parsed


def _extract_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                return part["text"]
    value = payload.get("output_text")
    return value if isinstance(value, str) else ""
