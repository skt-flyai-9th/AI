from __future__ import annotations

from app.ranker_core import gemini_json


def test_call_gemini_structured_builds_schema_request(monkeypatch):
    captured = {}

    def fake_request_json(session, method, url, **kwargs):
        captured["headers"] = dict(session.headers)
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs["json"]
        return {
            "candidates": [{
                "content": {"parts": [{"text": '{"ok": true}'}]},
                "finishReason": "STOP",
            }]
        }

    monkeypatch.setattr(gemini_json, "request_json", fake_request_json)
    result = gemini_json.call_gemini_structured(
        api_key="gem-test",
        model="gemini-2.5-flash-lite",
        system_prompt="system",
        user_prompt="user",
        schema_name="result",
        schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    )

    assert result == {"ok": True}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/gemini-2.5-flash-lite:generateContent")
    assert captured["headers"]["x-goog-api-key"] == "gem-test"
    cfg = captured["json"]["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["responseJsonSchema"]["required"] == ["ok"]


def test_call_gemini_structured_attaches_youtube_as_native_video(monkeypatch):
    captured = {}

    def fake_request_json(session, method, url, **kwargs):
        captured.update(kwargs)
        return {
            "candidates": [
                {"content": {"parts": [{"text": '{"summary":"ok"}'}]}}
            ]
        }

    monkeypatch.setattr(gemini_json, "resolve_gemini_model", lambda *_: "gemini-test")
    monkeypatch.setattr(gemini_json, "request_json", fake_request_json)
    result = gemini_json.call_gemini_structured(
        api_key="key",
        model="gemini-test",
        system_prompt="system",
        user_prompt="analyze",
        schema_name="video",
        schema={"type": "object"},
        file_uris=["https://www.youtube.com/watch?v=abc"],
    )
    parts = captured["json"]["contents"][0]["parts"]
    assert parts[0]["file_data"]["file_uri"].startswith("https://www.youtube.com/")
    assert parts[1] == {"text": "analyze"}
    assert result == {"summary": "ok"}
