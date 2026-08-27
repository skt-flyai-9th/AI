from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel

from app.core.config import get_settings
from app.ranker_core.gemini_json import call_gemini_structured, resolve_gemini_model
from app.schemas.template_knowledge import (
    VideoEditingDBContent,
    EditingVideoInsight,
    MAX_SHOOTING_GUIDE_CUTS,
    TradeAreaAnalysisResult,
    TradeAreaEvidence,
    TradeAreaDBContent,
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
    ) -> TradeAreaDBContent: ...

    def generate_editing(
        self,
        *,
        template_id: str,
        base_payload: dict[str, Any] | None,
        trend_context: list[dict[str, Any]],
        insights: list[EditingVideoInsight],
    ) -> VideoEditingDBContent: ...

    def analyze_trade_area(
        self,
        *,
        template: TradeAreaDBContent,
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
    """GPT application that proposes DB versions; deterministic code validates them."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.openai_api_key.strip()
        self.base_url = settings.openai_base_url.rstrip("/")
        self._model_name = settings.database_openai_model.strip()
        self.timeout = settings.database_request_timeout_seconds
        self.max_output_tokens = settings.database_max_output_tokens

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate_trade_area(
        self,
        *,
        template_id: str,
        base_payload: dict[str, Any] | None,
        evidence: TradeAreaEvidence,
    ) -> TradeAreaDBContent:
        return self._request(
            schema_model=TradeAreaDBContent,
            schema_name="trade_area_db_candidate",
            instructions=(
                "You maintain the SARILS trade-area DB. Produce a conservative, "
                "evidence-bound Korean commercial-area database version. Never infer an individual "
                "customer's attributes. Preserve useful existing rules, update only where the "
                "new evidence supports it, and make every rule machine-readable."
            ),
            payload={
                "task": "Create a new version candidate; never mutate the base version.",
                "database_id": template_id,
                "base_database": base_payload,
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
    ) -> VideoEditingDBContent:
        return self._request(
            schema_model=VideoEditingDBContent,
            schema_name="video_editing_db_candidate",
            instructions=(
                "You maintain the REALS video-editing DB. Generate one version candidate "
                "grounded only in the supplied trendcluster records and Gemini video insights. "
                "The video-editing DB schema is fixed: never add fields or columns. Preserve the "
                "reference-original segment context and reusable effect guidance inside existing "
                "shooting-guide scene descriptions/tasks and existing editing rules only. The "
                "product accepts user-recorded video only. TTS, narration synthesis, still photos "
                "on the timeline, and photo-to-video are forbidden. Use only renderer capabilities "
                "included in the payload."
            ),
            payload={
                "task": "Create a new version candidate; never mutate the base version or schema.",
                "database_id": template_id,
                "base_database": base_payload,
                "trend_context": trend_context,
                "gemini_video_insights": [item.model_dump(mode="json") for item in insights],
                "guide_authoring_rules": [
                    f"Create at most {MAX_SHOOTING_GUIDE_CUTS} ordered shooting-guide scenes and at most {MAX_SHOOTING_GUIDE_CUTS} matching tasks.",
                    "Every scene_dialogue must be at most 9 characters including spaces; use null when no spoken line is required.",
                    "Treat gemini_video_insights[].segments as the authoritative cut plan.",
                    "Create exactly one shooting-guide scene and one matching task for each authoritative segment, preserving sequence and semantic role.",
                    "For the task matching segment sequence N, set display_order to N and the zero-based scene_index to N-1.",
                    "Never merge segments across a visible edit discontinuity, even when adjacent segments have the same subject, action, or semantic role.",
                    "Write every user-facing name, recommendation, scene description, subtitle, task title, and instruction in natural Korean; keep only machine identifiers and effect IDs in English.",
                    "Include each segment's observed start/end timestamps and evidence in its scene description or task instructions so the cut boundary remains auditable.",
                    "Use Gemini's reference-original shot order and segment context as the target editing grammar.",
                    "When Gemini observes SHAKE, VIBRATION, ROTATION/TILT, ZOOM, POSITION_MOVE, FLASH, or COLOR, summarize when and why it occurs in existing scene_description/tasks; do not add schema fields.",
                    "Keep measurable effect values such as angle, amplitude, duration frames, scale, direction and damping in concise existing text fields when supported by evidence.",
                    "Do not copy reference caption wording; preserve only caption role/style/placement/timing grammar.",
                ],
                "renderer_contract": {
                    "source_type": "VIDEO_ONLY",
                    "render_profile_id": "INSTAGRAM_REELS_V1",
                    "assembly_profile_id": "INTERMEDIATE_VERTICAL_V1",
                    "safe_area_profile_id": "INSTAGRAM_REELS_2026_V1",
                    "allowed_effect_ids": [
                        "PUNCH_ZOOM",
                        "SMOOTH_ZOOM",
                        "SHAKE",
                        "VIBRATION",
                        "ROTATION",
                        "POSITION_MOVE",
                        "FLASH",
                        "COLOR",
                        "COLOR_TONE",
                    ],
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
        template: TradeAreaDBContent,
        evidence: TradeAreaEvidence,
    ) -> TradeAreaAnalysisResult:
        return self._request(
            schema_model=TradeAreaAnalysisResult,
            schema_name="trade_area_analysis",
            instructions=(
                "Apply the supplied SARILS trade-area DB version to aggregate evidence. "
                "Return only evidence-grounded area characteristics and audience ranges. "
                "Do not describe any identifiable person or claim inferred traits as facts."
            ),
            payload={
                "database": template.model_dump(mode="json"),
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
                "OPENAI_API_KEY or DATABASE_OPENAI_MODEL is not configured.", retryable=False
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
            raise TemplateKnowledgeLLMError("Database GPT request timed out.") from exc
        except httpx.HTTPError as exc:
            raise TemplateKnowledgeLLMError("Database GPT request failed.") from exc
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
                "Database GPT returned invalid structured output."
            ) from exc


class GeminiYouTubeVideoAnalyzer:
    """Analyze public YouTube references with Gemini's native video input."""

    def __init__(self) -> None:
        settings = get_settings()
        self.api_key = settings.gemini_api_key.strip()
        self._configured_model_name = settings.database_gemini_model.strip()
        self._resolved_model_name = ""
        self.timeout = settings.database_video_analysis_timeout_seconds

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
        reference_cut_review = _human_reviewed_reference_cut_review(trend_context)
        expected_cut_count = (
            reference_cut_review.get("expected_cut_count")
            if reference_cut_review is not None
            else None
        )
        prompt_payload = {
            "task": (
                    "Analyze the supplied public YouTube video as reference-original editing evidence "
                    "for a Korean small-business short-form video-editing database. Preserve the "
                    "original edit-cut order and describe the meaning of every cut. Analyze "
                    "observable hooks, pacing, captions, composition, camera motion, cut-transition "
                    "points and effects. For SHAKE, VIBRATION, ROTATION/TILT, ZOOM, POSITION_MOVE, "
                    "FLASH and COLOR, estimate measurable parameters when visually supportable: "
                    "timestamp/frame window, duration frames, direction, translation as frame %, "
                    "rotation degrees, scale, frequency, damping and color/tone. Put compact numeric "
                    "observations into camera_patterns, transition_patterns, reusable_editing_rules "
                    "and evidence_notes; the output schema must not be expanded. Describe when an "
                    "effect happens semantically (for example PRODUCT_REVEAL or IMPACT), not only "
                    "its appearance. Do not recommend TTS, generated narration, still-photo scenes, "
                    "platform UI reproduction, or unobserved content. First perform a frame-to-frame "
                    "discontinuity audit, then divide the complete reference "
                    f"into no more than {MAX_SHOOTING_GUIDE_CUTS} ordered edit cuts. "
                    "Return every cut in segments with explicit sequence, start_sec, end_sec, "
                    "scene_role, description, shot_type, transition_out and timestamped evidence. "
                    "segments is the authoritative cut plan; shot_sequence must contain the same "
                    "number of items in the same order. Do not merge two physical edit cuts merely "
                    "because they share one semantic role. A new segment is mandatory whenever an "
                    "object suddenly appears or disappears (including a food reveal), a person "
                    "suddenly enters or leaves, a person's pose or screen position jumps without "
                    "continuous motion, the background or camera framing discontinuously resets, "
                    "or a transition effect bridges two shots. These remain cut boundaries even "
                    "when the subject and action are otherwise unchanged. "
                    "Cuts must not overlap and must cover the observed content from the opening hook "
                    "through the final meaningful frame."
            ),
            "trend_id": trend_id,
            "youtube_url": youtube_url,
            "trend_context": trend_context,
            "effect_analysis_format_examples": [
                    "SEGMENT|id=seg_02|role=PROCESS|start=2.10s|end=4.80s|subject=drink|composition=close-up",
                    "EFFECT|segment=seg_03|event=PRODUCT_REVEAL|type=SHAKE|start=5.40s|duration=4f|x=1.8%|y=0.7%|rotation=0.5deg|scale=1.018|damping=true",
                    "EFFECT|segment=seg_01|event=HOOK|type=ROTATION|start=0.20s|duration=8f|rotation=-1.2deg",
            ],
            "cut_boundary_rules": [
                    "Start a new cut at every observable shot change, action-state discontinuity, subject change, or intentional transition boundary.",
                    "Treat a prop or food item popping into or out of view between adjacent frames as a mandatory cut boundary.",
                    "Treat a person disappearing, reappearing, teleporting, or jumping instantly to a different pose or screen position as a mandatory cut boundary.",
                    "A continuous action may stay in one cut only when the motion between frames is visually continuous.",
                    "Do not collapse multiple physical edit cuts into one semantic chapter.",
                    "Use timestamps from the supplied video; do not invent evenly spaced cuts.",
                    "Do not overlap segments or reverse their order.",
                    "Record the visual observation that justifies every boundary in segments[].evidence.",
            ],
            "human_reviewed_reference_cut_review": reference_cut_review,
            "allowed_audio_roles": ["PLATFORM_MUSIC", "ORIGINAL_AMBIENCE", "NONE"],
        }
        if expected_cut_count is not None:
            prompt_payload["task"] += (
                f" A human reviewer confirmed exactly {expected_cut_count} physical edit cuts. "
                "Return exactly that many segments and use the supplied boundary_basis to find the "
                "subtle discontinuities; do not create arbitrary evenly spaced cuts."
            )
        previous_insight: EditingVideoInsight | None = None
        for attempt in range(3 if expected_cut_count is not None else 1):
            if attempt:
                assert previous_insight is not None
                previous_count = len(previous_insight.segments)
                prompt_payload["previous_mismatched_cut_analysis"] = (
                    previous_insight.model_dump(mode="json")
                )
                prompt_payload["correction"] = (
                    f"The previous analysis returned {previous_count} cuts instead of the "
                    f"human-reviewed total of exactly {expected_cut_count}. Preserve every valid "
                    "boundary in previous_mismatched_cut_analysis, then inspect inside its segments "
                    "for the missed object/person/pose discontinuity. Split only at visually "
                    "supported discontinuities and return the exact reviewed total."
                )
            prompt = json.dumps(prompt_payload, ensure_ascii=False)
            try:
                parsed = call_gemini_structured(
                    api_key=self.api_key,
                    model=self.model_name,
                    system_prompt=(
                        "You are REALS's evidence-only reference-video analyst. Treat the original "
                        "video's segment context as the editing target that later user footage will be "
                        "matched against. Visual and effect measurements must be conservative, timestamped, "
                        "and reusable. Never invent a value that the video does not support."
                    ),
                    user_prompt=prompt,
                    schema_name="editing_video_insight",
                    schema=EditingVideoInsight.model_json_schema(),
                    timeout=self.timeout,
                    file_uris=[youtube_url],
                )
                parsed["trend_id"] = trend_id
                parsed["youtube_url"] = youtube_url
                insight = EditingVideoInsight.model_validate(parsed)
                if expected_cut_count is None or len(insight.segments) == expected_cut_count:
                    return insight
                previous_insight = insight
            except TemplateKnowledgeLLMError:
                raise
            except Exception as exc:
                raise TemplateKnowledgeLLMError("Gemini video analysis failed.") from exc
        raise TemplateKnowledgeLLMError(
            f"Gemini did not reproduce the human-reviewed {expected_cut_count}-cut boundary plan.",
            retryable=True,
        )


def _human_reviewed_reference_cut_review(
    trend_context: dict[str, Any],
) -> dict[str, Any] | None:
    raw_details = trend_context.get("raw_details")
    if not isinstance(raw_details, dict):
        return None
    review = raw_details.get("reference_cut_review")
    if not isinstance(review, dict) or review.get("status") != "HUMAN_REVIEWED":
        return None
    expected = review.get("expected_cut_count")
    if not isinstance(expected, int) or not 1 <= expected <= MAX_SHOOTING_GUIDE_CUTS:
        return None
    basis = review.get("boundary_basis")
    if not isinstance(basis, list) or not basis or not all(
        isinstance(item, str) and item.strip() for item in basis
    ):
        return None
    return {
        "status": "HUMAN_REVIEWED",
        "expected_cut_count": expected,
        "boundary_basis": basis,
    }


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
