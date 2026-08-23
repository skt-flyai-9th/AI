from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.ranker_core.discovery import discover_candidates_from_csv


def test_discovery_groups_template_rows(tmp_path: Path) -> None:
    rows = []
    for index in range(4):
        rows.append(
            {
                "challenge_id": "",
                "challenge_name": "",
                "platform": "capcut",
                "content_id": f"c{index}",
                "author_id": f"a{index}",
                "created_at": f"2026-08-{10 + index:02d}T12:00:00+09:00",
                "caption": "새로운 사진 전환 #사진전환",
                "hashtags": "#사진전환|#fyp",
                "audio_id": "audio-1",
                "effect_id": "",
                "template_id": "template-123",
                "views": 1000,
                "likes": 50,
                "comments": 2,
                "shares": 5,
                "is_paid": False,
                "kr_affinity": 0.95,
                "creator_followers": 1000,
                "creator_category": "transition",
            }
        )
    source = tmp_path / "raw.csv"
    pd.DataFrame(rows).to_csv(source, index=False)
    candidates, resolved = discover_candidates_from_csv(
        source,
        tmp_path / "candidates.csv",
        resolved_observations_output=tmp_path / "observations.csv",
        min_posts=3,
        min_authors=3,
    )
    assert len(candidates) == 1
    assert len(resolved) == 4
    assert candidates.iloc[0]["discovery_key"].startswith("template:")
    assert resolved["challenge_id"].nunique() == 1
