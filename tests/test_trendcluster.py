from __future__ import annotations

import json
from pathlib import Path

from app.agents.challenge_ranking.trendcluster import (
    TRENDCLUSTER_FILENAME,
    build_video_editing_db_trendcluster,
    sync_video_editing_db_trendcluster,
)


def test_checked_in_trendcluster_matches_provided_video_editing_db():
    expected = build_video_editing_db_trendcluster()
    checked_in = json.loads(
        Path("exports/trendcluster.json").read_text(encoding="utf-8")
    )

    assert checked_in == expected
    assert checked_in["count"] == 3
    assert [item["rank"] for item in checked_in["results"]] == [1, 2, 3]
    assert [item["name"] for item in checked_in["results"]] == [
        "주술회전 트랜지션",
        "카페 추천 리뷰 릴스",
        "오츠카레 썸머 챌린지",
    ]
    assert all(
        item["representative_youtube_url"] == item["guide_youtube_url"]
        for item in checked_in["results"]
    )
    assert checked_in["results"][1]["guide_youtube_url"] is None
    assert not Path("exports/ranking_latest.json").exists()


def test_sync_video_editing_db_trendcluster_is_atomic_and_deterministic(tmp_path):
    path = sync_video_editing_db_trendcluster(tmp_path)

    assert path.name == TRENDCLUSTER_FILENAME
    assert json.loads(path.read_text(encoding="utf-8")) == (
        build_video_editing_db_trendcluster()
    )
    assert not path.with_suffix(".json.tmp").exists()
