from __future__ import annotations

import pandas as pd
import requests

from app.ranker_core import auto_discovery


def test_one_failed_gemini_chunk_keeps_results_from_other_chunks(monkeypatch):
    calls: list[int] = []

    def _extract(_key, _model, chunk, _now):
        calls.append(len(chunk))
        if len(calls) == 2:
            raise RuntimeError("Gemini 429: rate limited")
        return {"challenges": [{"challenge_id": f"c{len(calls)}"}]}

    monkeypatch.setattr(auto_discovery, "_extract_chunk", _extract)
    records = [{"evidence_id": f"ev{i}"} for i in range(60)]

    extracted, errors, total = auto_discovery._extract_challenges_resilient(
        "key", "model", records, 20, pd.Timestamp("2026-08-30T00:00:00Z")
    )

    assert total == 3
    assert len(calls) == 3
    # Chunks 1 and 3 survive; only chunk 2's failure is recorded.
    assert [item["challenge_id"] for item in extracted] == ["c1", "c3"]
    assert len(errors) == 1
    assert "429" in errors[0]


def test_all_chunks_failing_reports_the_underlying_error(monkeypatch):
    monkeypatch.setattr(
        auto_discovery,
        "_extract_chunk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("quota exhausted")),
    )
    records = [{"evidence_id": f"ev{i}"} for i in range(40)]

    extracted, errors, total = auto_discovery._extract_challenges_resilient(
        "key", "model", records, 20, pd.Timestamp("2026-08-30T00:00:00Z")
    )

    assert extracted == []
    assert total == 2
    assert len(errors) == 2
    assert "quota exhausted" in errors[-1]


def test_youtube_seed_corpus_degrades_instead_of_raising(monkeypatch):
    def _failing_request(*_args, **_kwargs):
        raise RuntimeError("YouTube API 403: quotaExceeded")

    monkeypatch.setattr(auto_discovery, "request_json", _failing_request)

    records, status = auto_discovery._collect_youtube_seed_corpus(
        requests.Session(),
        "api-key",
        {"seed_queries": ["챌린지"], "max_search_requests": 3},
        pd.Timestamp("2026-08-30T00:00:00Z"),
    )

    assert records == []
    assert status["success"] is False
    assert status["rows"] == 0
    assert any("quotaExceeded" in item for item in status["errors"])


def test_youtube_detail_batch_failure_keeps_partial_search_results(monkeypatch):
    call_count = {"n": 0}

    def _request(_session, _method, url, params=None):
        call_count["n"] += 1
        if "search" in url:
            return {"items": [{"id": {"videoId": "AbCdEfGhI12"}}]}
        raise RuntimeError("videos.list 500")

    monkeypatch.setattr(auto_discovery, "request_json", _request)

    records, status = auto_discovery._collect_youtube_seed_corpus(
        requests.Session(),
        "api-key",
        {"seed_queries": ["챌린지"], "max_search_requests": 1, "viewcount_seed_count": 0},
        pd.Timestamp("2026-08-30T00:00:00Z"),
    )

    # The details batch failed, so no full records — but the collector returns
    # gracefully with the error recorded instead of crashing the whole run.
    assert records == []
    assert any("videos.list" in item for item in status["errors"])
