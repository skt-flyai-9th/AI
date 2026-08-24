from __future__ import annotations

import requests
import pytest

from app.ranker_core.utils import (
    redact_sensitive_data,
    redact_sensitive_text,
    request_json,
)


class ErrorSession:
    def __init__(self, response_url: str):
        self.response_url = response_url

    def request(self, method: str, url: str, **kwargs):
        response = requests.Response()
        response.status_code = 429
        response.reason = "Too Many Requests"
        response.url = self.response_url
        return response


def test_request_json_does_not_expose_query_credentials():
    secret = "provider-secret-value"
    session = ErrorSession(f"https://example.test/search?key={secret}&q=trend")

    with pytest.raises(RuntimeError) as exc_info:
        request_json(
            session,
            "GET",
            f"https://example.test/search?api_key={secret}",
            retries=0,
        )

    message = str(exc_info.value)
    assert secret not in message
    assert "?" not in message
    assert "status=429" in message


def test_redact_sensitive_data_covers_urls_json_and_headers():
    secret = "provider-secret-value"
    payload = {
        "error": (
            f"GET https://example.test/search?token={secret}&q=trend "
            f'body={{"client_secret":"{secret}"}} Authorization: Bearer {secret}'
        ),
        "api_key": secret,
        "nested": [{"message": f"https://example.test/?signature={secret}"}],
    }

    redacted = redact_sensitive_data(payload)
    serialized = str(redacted)

    assert secret not in serialized
    assert redacted["api_key"] == "[REDACTED]"
    assert serialized.count("[REDACTED]") >= 4
    assert secret not in redact_sensitive_text(payload["error"])
