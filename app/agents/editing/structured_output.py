from __future__ import annotations

import json
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError


class EditingLLMError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


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
        "reasoning": {"effort": "low"},
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    output_token_limit = max_output_tokens
    for attempt in range(1, max_attempts + 1):
        request_payload["max_output_tokens"] = output_token_limit
        try:
            response = _post_responses_api(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                request_payload=request_payload,
            )
        except httpx.TimeoutException as exc:
            error = EditingLLMError(
                _request_error_message(
                    schema_name=schema_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason="timeout",
                )
            )
            if attempt >= max_attempts:
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
            retryable = response.status_code == 429 or response.status_code >= 500
            error = EditingLLMError(
                _request_error_message(
                    schema_name=schema_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason=f"http_{response.status_code}",
                ),
                retryable=retryable,
            )
            if not retryable or attempt >= max_attempts:
                raise error
            _wait_before_retry(attempt)
            continue

        response_payload: dict[str, Any] = {}
        try:
            raw_response_payload = response.json()
            if not isinstance(raw_response_payload, dict):
                raise TypeError("Responses API payload must be an object.")
            response_payload = raw_response_payload
            if _response_requires_retry(response_payload):
                raise ValueError("Responses API did not complete with structured output.")
            output_text = _extract_output_text(response_payload)
            parsed = json.loads(output_text)
            return schema_model.model_validate(parsed)
        except (ValidationError, ValueError, TypeError) as exc:
            reason = _structured_output_failure_reason(response_payload, exc)
            error = EditingLLMError(
                _request_error_message(
                    schema_name=schema_name,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    reason=reason,
                ),
                retryable=attempt < max_attempts,
            )
            if attempt >= max_attempts:
                raise error from exc
            output_token_limit = min(20_000, output_token_limit + max_output_tokens)
            request_payload["instructions"] = _retry_instructions(instructions, reason)
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


def _wait_before_retry(attempt: int) -> None:
    time.sleep(min(0.5 * (2 ** (attempt - 1)), 2.0))


def _retry_instructions(instructions: str, reason: str) -> str:
    return (
        f"{instructions}\n\n"
        "The previous response could not be validated against the required JSON schema "
        f"({reason}). Return one complete JSON object only. Include every required field, "
        "respect every enum and numeric bound, and do not include commentary outside JSON."
    )


def _request_error_message(
    *,
    schema_name: str,
    attempt: int,
    max_attempts: int,
    reason: str,
) -> str:
    return (
        "Editing GPT request failed: "
        f"schema={schema_name}; reason={reason}; attempt={attempt}/{max_attempts}."
    )


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


_ModelT = TypeVar("_ModelT", bound=BaseModel)
