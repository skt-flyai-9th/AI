from __future__ import annotations

import pandas as pd

from app.ranker_core.representative import (
    extract_youtube_video_id,
    select_representative_youtube,
)


def test_extract_youtube_video_id_supports_common_urls() -> None:
    assert extract_youtube_video_id("https://www.youtube.com/watch?v=AbCdEfGhI12") == "AbCdEfGhI12"
    assert extract_youtube_video_id("https://youtu.be/AbCdEfGhI12?t=3") == "AbCdEfGhI12"
    assert extract_youtube_video_id("https://www.youtube.com/shorts/AbCdEfGhI12") == "AbCdEfGhI12"
    assert extract_youtube_video_id("youtube:AbCdEfGhI12") == "AbCdEfGhI12"
    assert extract_youtube_video_id("not-a-video-id") == ""


def test_representative_video_prefers_relevant_video() -> None:
    now = pd.Timestamp("2026-08-19T12:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "challenge_id": "dance",
                "name": "새 춤 챌린지",
                "alias_list": ["새 춤 챌린지", "#새춤"],
            }
        ]
    )
    rows = pd.DataFrame(
        [
            {
                "challenge_id": "dance",
                "platform": "youtube",
                "content_id": "A1234567890",
                "title": "전혀 다른 인기 영상",
                "caption": "일반 영상",
                "hashtags": "",
                "author_id": "channel-a",
                "channel_title": "A 채널",
                "created_at": now - pd.Timedelta(days=1),
                "views": 2_000_000,
                "likes": 20_000,
                "comments": 500,
                "shares": 0,
                "kr_affinity": 0.9,
                "is_paid": False,
                "source_origin": "youtube_api",
            },
            {
                "challenge_id": "dance",
                "platform": "youtube",
                "content_id": "B1234567890",
                "title": "새 춤 챌린지 따라하기",
                "caption": "#새춤 참여 영상",
                "hashtags": "#새춤",
                "author_id": "channel-b",
                "channel_title": "B 채널",
                "created_at": now - pd.Timedelta(hours=6),
                "views": 180_000,
                "likes": 20_000,
                "comments": 800,
                "shares": 0,
                "kr_affinity": 0.95,
                "is_paid": False,
                "source_origin": "youtube_api",
            },
        ]
    )

    selected = select_representative_youtube(candidates, rows, now)
    row = selected.iloc[0]
    assert row["representative_youtube_video_id"] == "B1234567890"
    assert row["representative_youtube_url"].endswith("B1234567890")
    assert row["representative_youtube_title"] == "새 춤 챌린지 따라하기"


def test_dual_video_selection_prefers_famous_rep_and_tutorial_guide() -> None:
    now = pd.Timestamp("2026-08-21T00:00:00Z")
    candidates = pd.DataFrame([
        {
            "challenge_id": "c1",
            "name": "BAD 챌린지",
            "alias_list": ["BAD 챌린지", "BAD dance"],
        }
    ])
    rows = pd.DataFrame([
        {
            "challenge_id": "c1",
            "platform": "youtube",
            "content_id": "POPULAR1234",
            "youtube_url": "https://www.youtube.com/watch?v=POPULAR1234",
            "title": "BAD 챌린지 공식 퍼포먼스",
            "caption": "BAD 챌린지 dance challenge",
            "hashtags": "BAD challenge",
            "matched_alias": "BAD 챌린지",
            "author_id": "famous",
            "channel_title": "Famous Artist",
            "created_at": now - pd.Timedelta(days=5),
            "views": 9_000_000,
            "likes": 500_000,
            "comments": 10_000,
            "kr_affinity": 0.8,
            "source_origin": "youtube_api",
        },
        {
            "challenge_id": "c1",
            "platform": "youtube",
            "content_id": "GUIDE123456",
            "youtube_url": "https://www.youtube.com/watch?v=GUIDE123456",
            "title": "BAD 챌린지 안무 거울모드 tutorial",
            "caption": "천천히 따라하는 dance practice",
            "hashtags": "BAD choreography",
            "matched_alias": "BAD 챌린지",
            "author_id": "teacher",
            "channel_title": "Dance Teacher",
            "created_at": now - pd.Timedelta(days=2),
            "views": 120_000,
            "likes": 9_000,
            "comments": 300,
            "kr_affinity": 0.95,
            "source_origin": "youtube_api",
        },
    ])
    selected = select_representative_youtube(
        candidates,
        rows,
        now,
        {"enabled": True, "ai_participation_check": False},
    ).iloc[0]
    assert selected["representative_youtube_video_id"] == "POPULAR1234"
    assert selected["guide_youtube_video_id"] == "GUIDE123456"
