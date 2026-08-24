from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel

from app.core.config import get_settings
from app.ranker_core.gemini_json import call_gemini_structured, resolve_gemini_model
from app.schemas.template_knowledge import (
    EditingTemplateContent,
    EditingVideoInsight,
    TradeAreaAnalysisResult,
    TradeAreaEvidence,
    TradeAreaTemplateContent,
)


class TemplateKnowledgeLLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class TemplateCandidateGenerator(Protocol):
    @property
    def model_name(self) -> str: ...

    def generate_trade_area(
        self,
        *,
        template_id: str,
        base_payload: dict[str, Any] | None,
        evidence: TradeAreaEvidence,
    ) -> TradeAreaTemplateContent: ...

    def generate_editing(
        self,
        *,
        template_id: str,
        base_payload: dict[str, Any] | None,
        trend_context: list[dict[str, Any]],
        insights: list[EditingVideoInsight],
    ) -> EditingTemplateContent: ...

    def analyze_trade_area(
        self,
        *,
        template: TradeAreaTemplateContent,
        evidence: TradeAreaEvidence,
    ) -> TradeAreaAnalysisResult: ...


class ReferenceVideoAnalyzer(Protocol):
    @property
    def model_name(self) -> str: ...

    def analyze(
        self,
        *,
        trend_id: str,
        youtube_url: str,
        trend_context: dict[str, Any],
    ) -> EditingVideoInsight: ...


class OpenAITemplateCandidateGenerator:
    """GPT application that proposes templates; deterministic code validates them."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.openai_api_key.strip()
        self.base_url = settings.openai_base_url.rstrip("/")
        self._model_name = settings.template_openai_model.strip()
        self.timeout = settings.template_request_timeout_seconds
        self.max_output_tokens = settings.template_max_output_tokens

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate_trade_area(
        self,
        *,
        template_id: str,
        base_payload: dict[str, Any] | None,
        evidence: TradeAreaEvidence,
    ) -> TradeAreaTemplateContent:
        return self._request(
            schema_model=TradeAreaTemplateContent,
            schema_name="trade_area_template_candidate",
            instructions=(
                "You maintain SARILS trade-area analysis templates. Produce a conservative, "
                "evidence-bound Korean commercial-area template. Never infer an individual "
                "customer's attributes. Preserve useful existing rules, update only where the "
                "new evidence supports it, and make every rule machine-readable."
            ),
            payload={
                "task": "Create a new version candidate; never mutate the base version.",
                "template_id": template_id,
                "base_template": base_payload,
                "new_evidence": evidence.model_dump(mode="json"),
                "hard_policy": {
                    "aggregate_only": True,
                    "no_individual_attribute_assertions": True,
                    "cite_evidence_keys_in_rules": True,
                },
            },
        )

    def generate_editing(
        self,
        *,
        template_id: str,
        base_payload: dict[str, Any] | None,
        trend_context: list[dict[str, Any]],
        insights: list[EditingVideoInsight],
    ) -> EditingTemplateContent:
        return self._request(
            schema_model=EditingTemplateContent,
            schema_name="editing_template_candidate",
            instructions=(
                "You maintain SARILS video-editing templates. Generate one version candidate "
                "grounded only in the supplied Trend Research records and Gemini video insights. "
                "The product accepts user-recorded video only. TTS, narration synthesis, still "
                "photos on the timeline, and photo-to-video are forbidden. Use only renderer "
                "capabilities included in the payload."
            ),
            payload={
                "task": "Create a new version candidate; never mutate the base version.",
                "template_id": template_id,
                "base_template": base_payload,
                "trend_context": trend_context,
                "gemini_video_insights": [item.model_dump(mode="json") for item in insights],
                "renderer_contract": {
                    "source_type": "VIDEO_ONLY",
                    "render_profile_id": "INSTAGRAM_REELS_V1",
                    "assembly_profile_id": "INTERMEDIATE_VERTICAL_V1",
                    "safe_area_profile_id": "INSTAGRAM_REELS_2026_V1",
                    "allowed_effect_ids": ["PUNCH_ZOOM", "COLOR_TONE", "SMOOTH_ZOOM"],
                    "allowed_transition_ids": ["CUT", "HARD_CUT", "FLASH_WHITE"],
                    "audio_policy": "SILENT_V1",
                    "max_duration_sec": 60,
                    "min_cut_duration_ms": 300,
                },
            },
        )

    def analyze_trade_area(
        self,
        *,
        template: TradeAreaTemplateContent,
        evidence: TradeAreaEvidence,
    ) -> TradeAreaAnalysisResult:
        return self._request(
            schema_model=TradeAreaAnalysisResult,
            schema_name="trade_area_analysis",
            instructions=(
                "Apply the supplied SARILS trade-area template to aggregate evidence. "
                "Return only evidence-grounded area characteristics and audience ranges. "
                "Do not describe any identifiable person or claim inferred traits as facts."
            ),
            payload={
                "template": template.model_dump(mode="json"),
                "evidence": evidence.model_dump(mode="json"),
            },
        )

    def _request(
        self,
        *,
        schema_model: type[_ModelT],
        schema_name: str,
        instructions: str,
        payload: dict[str, Any],
    ) -> _ModelT:
        if not self.api_key or not self.model_name:
            raise TemplateKnowledgeLLMError(
                "OPENAI_API_KEY or TEMPLATE_OPENAI_MODEL is not configured.", retryable=False
            )
        schema = _make_strict_schema(schema_model.model_json_schema())
        request_payload = {
            "model": self.model_name,
            "instructions": instructions,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": json.dumps(payload, ensure_ascii=False, default=str),
                        }
                    ],
                }
            ],
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
            raise TemplateKnowledgeLLMError("Template GPT request timed out.") from exc
        except httpx.HTTPError as exc:
            raise TemplateKnowledgeLLMError("Template GPT request failed.") from exc
        if response.status_code >= 400:
            retryable = response.status_code == 429 or response.status_code >= 500
            raise TemplateKnowledgeLLMError(
                f"OpenAI Responses API returned HTTP {response.status_code}.",
                retryable=retryable,
            )
        try:
            parsed = json.loads(_extract_output_text(response.json()))
            return schema_model.model_validate(parsed)
        except (TypeError, ValueError) as exc:
            raise TemplateKnowledgeLLMError(
                "Template GPT returned invalid structured output."
            ) from exc


class GeminiYouTubeVideoAnalyzer:
    """Analyze public YouTube references with Gemini's native video input."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.gemini_api_key.strip()
        self._configured_model_name = settings.template_gemini_model.strip()
        self._resolved_model_name = ""
        self.timeout = settings.template_video_analysis_timeout_seconds

    @property
    def model_name(self) -> str:
        if self._resolved_model_name:
            return self._resolved_model_name
        if not self.api_key:
            return self._configured_model_name
        try:
            self._resolved_model_name = resolve_gemini_model(
                self.api_key, self._configured_model_name
            )
        except Exception:
            return self._configured_model_name
        return self._resolved_model_name

    def analyze(
        self,
        *,
        trend_id: str,
        youtube_url: str,
        trend_context: dict[str, Any],
    ) -> EditingVideoInsight:
        if not self.api_key:
            raise TemplateKnowledgeLLMError("GEMINI_API_KEY is not configured.", retryable=False)
        prompt = json.dumps(
            {
                "task": (
                    "Analyze the supplied public YouTube video as editing evidence for a Korean "
                    "small-business short-form template. Describe observable hooks, shot order, "
                    "pacing, captions, camera, transitions, and reusable editing rules with "
                    "timestamps in evidence_notes where possible. Do not recommend TTS, generated "
                    "narration, still-photo scenes, or unobserved content."
                ),
                "trend_id": trend_id,
                "youtube_url": youtube_url,
                "trend_context": trend_context,
                "allowed_audio_roles": ["PLATFORM_MUSIC", "ORIGINAL_AMBIENCE", "NONE"],
            },
            ensure_ascii=False,
        )
        try:
            parsed = call_gemini_structured(
                api_key=self.api_key,
                model=self.model_name,
                system_prompt=(
                    "You are SARILS's evidence-only reference-video analyst. "
                    "Visual observations must be conservative and reusable as editing rules."
                ),
                user_prompt=prompt,
                schema_name="editing_video_insight",
                schema=EditingVideoInsight.model_json_schema(),
                timeout=self.timeout,
                file_uris=[youtube_url],
            )
            parsed["trend_id"] = trend_id
            parsed["youtube_url"] = youtube_url
            return EditingVideoInsight.model_validate(parsed)
        except TemplateKnowledgeLLMError:
            raise
        except Exception as exc:
            raise TemplateKnowledgeLLMError("Gemini video analysis failed.") from exc


_ModelT = TypeVar("_ModelT", bound=BaseModel)


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
                value = part.get("text")
                if isinstance(value, str):
                    return value
    value = payload.get("output_text")
    return value if isinstance(value, str) else ""
