from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.core.config import get_settings

_PLACEHOLDER_KEYS = {
    "change-me-before-production",
    "change-this",
    "replace-with-a-long-random-value",
}


def require_internal_api_key(
    x_internal_api_key: str | None = Header(default=None, alias="X-Internal-API-Key"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Authenticate calls from the main backend.

    X-Internal-API-Key is canonical. X-Admin-Token remains available only for
    backward compatibility with the first challenge-ranker deployment.
    """

    settings = get_settings()
    expected = settings.effective_internal_api_key

    if not expected or expected in _PLACEHOLDER_KEYS:
        if settings.app_env.lower() in {"local", "test"}:
            return
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="INTERNAL_API_KEY is not configured on the AI server.",
        )

    presented = (x_internal_api_key or x_admin_token or "").strip()
    if not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A valid X-Internal-API-Key header is required.",
        )


# Compatibility alias for existing imports and clients.
require_admin_token = require_internal_api_key
