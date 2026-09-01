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
    checked_in = json.loads(Path("exports/trendcluster.json").read_text(encoding="utf-8"))

    assert checked_in == expected
    assert checked_in["count"] == 4
    assert [item["rank"] for item in checked_in["results"]] == [1, 2, 3, 4]
    assert [item["name"] for item in checked_in["results"]] == [
        "주술회전 트랜지션",
        "동그리오 챌린지",
        "오츠카레 썸머 챌린지",
        "도마 BAD 챌린지",
    ]
    assert [item["category"] for item in checked_in["results"]] == [
        "meme",
        "food",
        "challenge",
        "challenge",
    ]
    assert all(
        item["representative_youtube_url"] == item["guide_youtube_url"]
        for item in checked_in["results"]
    )
    assert checked_in["results"][0]["representative_youtube_url"] == (
        "https://www.youtube.com/shorts/Aa-CGr9-c8E"
    )
    assert checked_in["results"][0]["guide_youtube_url"] == (
        "https://www.youtube.com/shorts/Aa-CGr9-c8E"
    )
    assert checked_in["results"][1]["guide_youtube_url"] == (
        "https://www.youtube.com/shorts/iWyRoIJheV4"
    )
    assert checked_in["results"][3]["guide_youtube_url"] == (
        "https://www.youtube.com/shorts/rUIEHnyoPrU"
    )
    assert checked_in["results"][0]["reference_cut_review"]["expected_cut_count"] == 8
    assert checked_in["results"][1]["reference_cut_review"]["expected_cut_count"] == 7
    assert checked_in["results"][2]["reference_cut_review"]["expected_cut_count"] == 7
    assert checked_in["results"][3]["reference_cut_review"]["expected_cut_count"] == 1
    assert [
        (
            item["format_type"],
            item["expected_duration_sec"],
            item["shooting_difficulty"],
            item["requires_face"],
        )
        for item in checked_in["results"]
    ] == [
        ("해시태그", 14, "중", True),
        ("해시태그", 13, "중", True),
        ("해시태그", 13, "중", True),
        ("챌린지", 11, "중", True),
    ]
    assert not Path("exports/ranking_latest.json").exists()


def test_sync_video_editing_db_trendcluster_is_atomic_and_deterministic(tmp_path):
    path = sync_video_editing_db_trendcluster(tmp_path)

    assert path.name == TRENDCLUSTER_FILENAME
    assert json.loads(path.read_text(encoding="utf-8")) == (build_video_editing_db_trendcluster())
    assert not path.with_suffix(".json.tmp").exists()


def test_packaged_sources_do_not_contain_removed_shortforms():
    markers = (
        "cafe_recommendation_reels",
        "donggeurio_store_promotion",
        "gt_cafe_recommendation",
        "gt_donggeurio_store_promotion",
    )
    for path in (
        Path("app/template_knowledge/sources/video_editing.json"),
        Path("app/template_knowledge/sources/video_editing_task_intervals.json"),
        Path("exports/trendcluster.json"),
    ):
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in markers)
