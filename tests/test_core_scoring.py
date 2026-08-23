from __future__ import annotations

import pandas as pd

from app.ranker_core.scoring import score_challenges


CONFIG = {
    "confidence_floor_multiplier": 0.60,
    "seeded_penalty_max": 15.0,
    "emerging_weights": {
        "participation_acceleration": 0.25,
        "kr_creator_growth": 0.20,
        "creator_diversity": 0.15,
        "cross_platform": 0.15,
        "search_lift": 0.10,
        "freshness": 0.10,
        "participation_conversion": 0.05,
    },
    "mainstream_weights": {
        "unique_creators": 0.25,
        "reach": 0.20,
        "persistence": 0.15,
        "cross_platform": 0.15,
        "spillover": 0.10,
        "creator_diversity": 0.10,
        "organic_breadth": 0.05,
    },
}


def _base(challenge_id: str, name: str) -> dict:
    return {
        "challenge_id": challenge_id,
        "name": name,
        "category": "dance",
        "discovered_at": pd.Timestamp("2026-08-10T00:00:00Z"),
        "participation_acceleration": 1.5,
        "kr_creator_growth": 1.2,
        "unique_creators_7d": 100,
        "posts_7d": 120,
        "views_7d": 1_000_000,
        "engagement_rate_7d": 0.05,
        "adjusted_reach": 20,
        "creator_diversity": 0.8,
        "cross_platform_count": 3,
        "search_lift": 1.2,
        "search_acceleration": 0.8,
        "participation_conversion": 0.015,
        "persistence": 0.7,
        "organic_breadth": 0.8,
        "paid_ratio": 0.05,
        "creator_concentration": 0.2,
        "seeded_penalty": 0.1,
        "kr_affinity": 0.95,
        "age_days": 9,
        "freshness": 0.65,
        "news_7d": 5,
        "news_growth": 1.0,
        "x_posts_7d": 100,
        "spillover": 15,
        "sample_size": 120,
        "evidence_source_count": 5,
        "source_coverage_ratio": 1.0,
        "confidence": 90,
    }


def test_domestic_affinity_and_seed_penalty_affect_final_rank() -> None:
    domestic = _base("domestic", "국내")
    global_only = _base("global", "해외")
    global_only["kr_affinity"] = 0.2
    seeded = _base("seeded", "시딩")
    seeded["paid_ratio"] = 0.9
    seeded["creator_concentration"] = 0.9
    seeded["seeded_penalty"] = 0.9

    scored = score_challenges(pd.DataFrame([domestic, global_only, seeded]), CONFIG)
    ranks = scored.set_index("challenge_id")["final_rank"].to_dict()
    assert ranks["domestic"] < ranks["global"]
    assert ranks["domestic"] < ranks["seeded"]
