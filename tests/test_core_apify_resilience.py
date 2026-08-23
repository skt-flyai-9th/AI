from __future__ import annotations

import app.ranker_core.connectors.apify_instagram as mod


def test_resilient_popular_search_skips_bad_seed(monkeypatch):
    def fake_run_actor_items(*, token, actor_id, run_input, timeout_seconds, max_attempts=3):
        term = run_input["search"]
        if term == "bad":
            raise RuntimeError("blocked")
        return [{"id": term, "shortCode": term, "caption": term}]

    monkeypatch.setattr(mod, "run_actor_items", fake_run_actor_items)
    items, report = mod.collect_popular_reels_resilient(
        token="x", seeds=["bad", "good"], search_limit=2, max_seed_runs=2
    )
    assert len(items) == 1
    assert items[0]["id"] == "good"
    assert report["successful_terms"] == 1
    assert report["failed"][0]["term"] == "bad"
