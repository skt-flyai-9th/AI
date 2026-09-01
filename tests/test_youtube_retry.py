from __future__ import annotations

import pandas as pd

from app.ranker_core.connectors import youtube


def test_youtube_connector_retries_with_a_fallback_query_when_first_search_is_empty(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_request_json(_session, _method, url, *, params):
        if url == youtube.YouTubeConnector.search_url:
            calls.append(str(params["q"]))
            if len(calls) == 1:
                return {"items": []}
            return {"items": [{"id": {"videoId": "ABCDEFGHIJK"}}]}
        if url == youtube.YouTubeConnector.videos_url:
            return {
                "items": [
                    {
                        "id": "ABCDEFGHIJK",
                        "snippet": {
                            "publishedAt": "2026-08-31T00:00:00Z",
                            "title": "새 춤 챌린지 Shorts",
                            "description": "새 춤 챌린지 따라하기",
                            "tags": ["새춤챌린지"],
                            "channelTitle": "creator",
                        },
                        "statistics": {"viewCount": "1000"},
                        "contentDetails": {"duration": "PT15S"},
                    }
                ]
            }
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(youtube, "request_json", fake_request_json)
    connector = youtube.YouTubeConnector(
        {
            "max_search_requests": 3,
            "max_challenges": 1,
            "search_attempts_per_challenge": 3,
        }
    )
    candidates = pd.DataFrame(
        [{"challenge_id": "new-dance", "name": "새 춤", "alias_list": ["새 춤"]}]
    )

    rows = connector._collect_rows(
        candidates,
        pd.Timestamp("2026-09-01T00:00:00Z"),
        "test-key",
    )

    assert connector.search_request_count == 2
    assert len(calls) == 2
    assert "챌린지 shorts" in calls[1]
    assert rows.iloc[0]["youtube_url"] == "https://www.youtube.com/watch?v=ABCDEFGHIJK"
