from __future__ import annotations

import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.agents.editing.telemetry import record_request, record_response


class EditingLLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.reason = reason


def request_structured_model(
    *,
    schema_model: type[_ModelT],
    schema: dict[str, Any],
    base_url: str,
    api_key: str,
    model: str,
    instructions: str,
    content: list[dict[str, Any]],
    schema_name: str,
    timeout: int,
    max_output_tokens: int,
    max_attempts: int,
    rate_limit_retry_base_seconds: float,
    timeout_max_attempts: int | None = None,
) -> _ModelT:
    request_payload: dict[str, Any] = {
        "model": model,
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
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    output_token_limit = max_output_tokens
    timeout_attempt_limit = (
        max_attempts
        if timeout_max_attempts is None
        else max(1, min(timeout_max_attempts, max_attempts))
    )
    timeout_attempts = 0
    for attempt in range(1, max_attempts + 1):
        request_payload["max_output_tokens"] = output_token_limit
        try:
            record_request()
            response = _post_responses_api(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                request_payload=request_payload,
            )
        except httpx.TimeoutException as exc:
            timeout_attempts += 1
            error = EditingLLMError(
                _request_error_message(
                    schema_name=schema_name,
                    attempt=timeout_attempts,
                    max_attempts=timeout_attempt_limit,
                    reason="timeout",
                ),
                reason="timeout",
            )
            if timeout_attempts >= timeout_attempt_limit:
                raise error from exc
            _wait_before_retry(attempt)
            continue
        except httpx.HTTPError as exc:
            error = EditingLLMError(
                _request_error_message(
                    schema_name=schema_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason="network_error",
                )
            )
            if attempt >= max_attempts:
                raise error from exc
            _wait_before_retry(attempt)
            continue

        if response.status_code >= 400:
            rate_limit_code = _rate_limit_error_code(response)
            retryable = (
                response.status_code == 429
                and rate_limit_code not in {"insufficient_quota", "credit_balance_exhausted"}
            ) or response.status_code >= 500
            reason = f"http_{response.status_code}"
            if rate_limit_code:
                reason = f"{reason}_{rate_limit_code}"
            error = EditingLLMError(
                _request_error_message(
                    schema_name=schema_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason=reason,
                ),
                retryable=retryable,
            )
            if not retryable or attempt >= max_attempts:
                raise error
            if response.status_code == 429:
                _wait_before_retry(
                    attempt,
                    minimum_seconds=_rate_limit_delay_seconds(
                        response,
                        attempt=attempt,
                        base_seconds=rate_limit_retry_base_seconds,
                    ),
                )
            else:
                _wait_before_retry(attempt)
            continue

        response_payload: dict[str, Any] = {}
        try:
            raw_response_payload = response.json()
            if not isinstance(raw_response_payload, dict):
                raise TypeError("Responses API payload must be an object.")
            response_payload = raw_response_payload
            record_response(response_payload)
            if _response_requires_retry(response_payload):
                raise ValueError("Responses API did not complete with structured output.")
            output_text = _extract_output_text(response_payload)
            parsed = json.loads(output_text)
            parsed = _repair_known_editing_plan_omissions(parsed, schema_name=schema_name)
            return schema_model.model_validate(parsed)
        except (ValidationError, ValueError, TypeError) as exc:
            reason = _structured_output_failure_reason(response_payload, exc)
            detail = _structured_output_failure_detail(exc)
            error = EditingLLMError(
                _request_error_message(
                    schema_name=schema_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason=reason,
                    detail=detail,
                ),
                retryable=attempt < max_attempts,
            )
            if attempt >= max_attempts:
                raise error from exc
            output_token_limit = min(20_000, output_token_limit + max_output_tokens)
            request_payload["instructions"] = _retry_instructions(
                instructions,
                reason,
                detail=detail,
                schema_name=schema_name,
            )
            _wait_before_retry(attempt)

    raise EditingLLMError(
        _request_error_message(
            schema_name=schema_name,
            attempt=max_attempts,
            max_attempts=max_attempts,
            reason="retry_exhausted",
        ),
        retryable=False,
    )


def _post_responses_api(
    *,
    base_url: str,
    api_key: str,
    timeout: int,
    request_payload: dict[str, Any],
) -> httpx.Response:
    with httpx.Client(timeout=max(timeout, 60)) as client:
        return client.post(
            f"{base_url}/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=request_payload,
        )


def _wait_before_retry(attempt: int, *, minimum_seconds: float = 0.0) -> None:
    time.sleep(max(minimum_seconds, min(0.5 * (2 ** (attempt - 1)), 2.0)))


def _rate_limit_delay_seconds(
    response: httpx.Response,
    *,
    attempt: int,
    base_seconds: float,
) -> float:
    retry_after = 0.0
    try:
        headers = getattr(response, "headers", {}) or {}
        retry_after = float(headers.get("retry-after", "0"))
    except (TypeError, ValueError):
        pass
    return max(retry_after, min(base_seconds * (2 ** (attempt - 1)), 120.0))


def _rate_limit_error_code(response: httpx.Response) -> str:
    if response.status_code != 429:
        return ""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return ""
    return str(error.get("code") or error.get("type") or "").strip().lower()


def _retry_instructions(
    instructions: str,
    reason: str,
    *,
    detail: str = "",
    schema_name: str = "",
) -> str:
    outcome_contract = ""
    if schema_name in {"editing_plan", "editing_plan_repair"}:
        outcome_contract = (
            " For outcome=RECIPE, recipe and publishing are required and "
            "missing_scene_roles/available_options must both be empty. For "
            "outcome=SOURCE_GAP, recipe and publishing must be null, "
            "missing_scene_roles must be non-empty, and available_options must contain "
            "USE_REDUCED_STRUCTURE and ADD_MORE_VIDEO exactly once each."
        )
    feedback = detail or reason
    return (
        f"{instructions}\n\n"
        "The previous response could not be validated against the required JSON schema "
        f"({reason}). Validation feedback: {feedback}. Return one complete JSON object only. "
        "Include every required field, respect every enum and numeric bound, and do not "
        f"include commentary outside JSON.{outcome_contract}"
    )


def _request_error_message(
    *,
    schema_name: str,
    attempt: int,
    max_attempts: int,
    reason: str,
    detail: str = "",
) -> str:
    message = (
        "Editing GPT request failed: "
        f"schema={schema_name}; reason={reason}; attempt={attempt}/{max_attempts}."
    )
    if detail:
        message += f" detail={detail}"
    return message


def _structured_output_failure_reason(payload: dict[str, Any], exc: Exception) -> str:
    status = str(payload.get("status") or "").strip().lower()
    if status == "incomplete":
        details = payload.get("incomplete_details")
        reason = details.get("reason") if isinstance(details, dict) else None
        return f"incomplete_{str(reason or 'unknown').lower()}"
    if status and status != "completed":
        return f"response_status_{status}"
    if _extract_refusal(payload):
        return "refusal"
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if not errors:
            return "schema_validation"
        first = errors[0]
        location = ".".join(str(item) for item in first.get("loc", [])) or "root"
        error_type = str(first.get("type") or "invalid")
        return f"schema_validation_{location}_{error_type}"[:300]
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if not _extract_output_text(payload).strip():
        return "empty_output"
    return "invalid_structured_output"


def _structured_output_failure_detail(exc: Exception) -> str:
    """Return actionable validation feedback without echoing model input or URLs."""
    if isinstance(exc, ValidationError):
        details: list[str] = []
        for error in exc.errors(include_url=False, include_input=False)[:5]:
            location = ".".join(str(item) for item in error.get("loc", [])) or "root"
            message = " ".join(str(error.get("msg") or "invalid value").split())
            details.append(f"{location}: {message}"[:300])
        return "; ".join(details)[:1000]
    if isinstance(exc, json.JSONDecodeError):
        return f"invalid JSON at line {exc.lineno}, column {exc.colno}"
    return " ".join(str(exc).split())[:500]


def _response_requires_retry(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or "").strip().lower()
    return (bool(status) and status != "completed") or bool(_extract_refusal(payload))


def _extract_refusal(payload: dict[str, Any]) -> str:
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for part in item.get("content", []):
            if not isinstance(part, dict) or part.get("type") != "refusal":
                continue
            value = part.get("refusal")
            if isinstance(value, str):
                return value
    return ""


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


def _repair_known_editing_plan_omissions(
    parsed: Any,
    *,
    schema_name: str,
) -> Any:
    """Repair a safe publishing-title omission without inventing new facts.

    Some Responses API completions have omitted only ``publishing.title`` even
    under strict schema mode. The post caption and CTA are already constrained
    to evidence-safe marketing copy, so reuse one of them as the title and let
    normal Pydantic validation reject every other malformed field.
    """
    if schema_name not in {"editing_plan", "editing_plan_repair"}:
        return parsed
    if not isinstance(parsed, dict) or parsed.get("outcome") != "RECIPE":
        return parsed
    publishing = parsed.get("publishing")
    if not isinstance(publishing, dict) or str(publishing.get("title") or "").strip():
        return parsed

    candidates = [publishing.get("caption")]
    recipe = parsed.get("recipe")
    if isinstance(recipe, dict):
        cta = recipe.get("cta")
        if isinstance(cta, dict):
            candidates.append(cta.get("text"))
    for candidate in candidates:
        title = " ".join(str(candidate or "").split())[:80].rstrip()
        if title:
            publishing["title"] = title
            break
    return parsed


_ModelT = TypeVar("_ModelT", bound=BaseModel)
