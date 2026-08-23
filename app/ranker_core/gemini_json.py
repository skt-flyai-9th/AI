from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

import requests

from .utils import request_json

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "User-Agent": "challenge-ranker-gemini/2.0",
    }


@lru_cache(maxsize=8)
def resolve_gemini_model(api_key: str, requested: str = "auto") -> str:
    requested = (requested or "auto").strip()
    if requested != "auto":
        return requested.replace("models/", "")

    session = requests.Session()
    session.headers.update(_headers(api_key))
    payload = request_json(session, "GET", f"{GEMINI_BASE}/models", timeout=30, retries=2)
    available: list[str] = []
    for model in payload.get("models", []):
        name = str(model.get("name", "")).replace("models/", "")
        methods = set(model.get("supportedGenerationMethods", []) or [])
        if name and (not methods or "generateContent" in methods):
            available.append(name)

    env_preferred = os.getenv("GEMINI_MODEL", "").strip()
    preferred = [
        env_preferred,
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
        "gemini-3-flash-preview",
        "gemini-2.5-flash",
    ]
    for candidate in preferred:
        if candidate and candidate in available:
            return candidate

    flash_lite = [m for m in available if "flash-lite" in m and "preview" not in m]
    if flash_lite:
        return sorted(flash_lite, reverse=True)[0]
    flash = [m for m in available if "flash" in m and "preview" not in m]
    if flash:
        return sorted(flash, reverse=True)[0]
    if available:
        return available[0]
    raise RuntimeError("Gemini Models API에서 generateContent 가능한 모델을 찾지 못했습니다.")


def call_gemini_structured(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    schema: dict[str, Any],
    timeout: float = 120.0,
) -> dict[str, Any]:
    del schema_name
    resolved_model = resolve_gemini_model(api_key, model)
    session = requests.Session()
    session.headers.update(_headers(api_key))
    url = f"{GEMINI_BASE}/models/{resolved_model}:generateContent"

    request_body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": schema,
            "temperature": 0.1,
        },
    }
    try:
        payload = request_json(
            session, "POST", url, timeout=timeout, retries=4, json=request_body
        )
    except RuntimeError as exc:
        # Some otherwise usable Gemini models can reject responseJsonSchema.
        # Fall back to JSON mime type while keeping the same prompt/schema description.
        fallback_body = dict(request_body)
        fallback_body["generationConfig"] = {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        }
        if "400" not in str(exc):
            raise
        payload = request_json(
            session, "POST", url, timeout=timeout, retries=4, json=fallback_body
        )

    prompt_feedback = payload.get("promptFeedback", {}) or {}
    if prompt_feedback.get("blockReason"):
        raise RuntimeError(f"Gemini가 프롬프트를 차단했습니다: {prompt_feedback.get('blockReason')}")
    candidates = payload.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini 응답에 candidates가 없습니다.")
    parts = (candidates[0].get("content", {}) or {}).get("parts", []) or []
    text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict)).strip()
    if not text:
        raise RuntimeError(f"Gemini 응답에 JSON text가 없습니다. finishReason={candidates[0].get('finishReason', '')}")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Gemini 구조화 응답 JSON 파싱에 실패했습니다.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini 구조화 응답 최상위가 객체가 아닙니다.")
    return parsed
