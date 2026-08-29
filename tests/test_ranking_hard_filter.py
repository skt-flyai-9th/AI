from __future__ import annotations

import pandas as pd

from app.ranker_core import ai_analysis
from app.ranker_core.pipeline import _build_public_ranking
from app.ranker_core.representative import select_representative_youtube


def test_hard_filtered_rows_rank_after_every_accepted_challenge(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai_analysis,
        "call_gemini_structured",
        lambda **kwargs: {
            "judgements": [
                {
                    "challenge_id": "viral_news",
                    "is_social_challenge": False,
                    "trend_score": 95.0,
                    "domestic_relevance": 0.9,
                    "evidence_quality": 0.9,
                    "reason": "뉴스 해설 영상",
                },
                {
                    "challenge_id": "weak_challenge",
                    "is_social_challenge": True,
                    "trend_score": 8.0,
                    "domestic_relevance": 0.5,
                    "evidence_quality": 0.4,
                    "reason": "약하지만 실제 참여형",
                },
            ]
        },
    )
    ranking = pd.DataFrame(
        [
            {
                "challenge_id": "viral_news",
                "name": "바이럴 뉴스",
                "final_score": 95.0,
                "confidence": 0.9,
                "kr_affinity": 0.9,
            },
            {
                "challenge_id": "weak_challenge",
                "name": "약한 챌린지",
                "final_score": 8.0,
                "confidence": 0.4,
                "kr_affinity": 0.5,
            },
        ]
    )
    candidates = pd.DataFrame(
        [
            {
                "challenge_id": "viral_news",
                "aliases": "",
                "entity_confidence": 0.9,
                "kr_affinity_hint": 0.9,
            },
            {
                "challenge_id": "weak_challenge",
                "aliases": "",
                "entity_confidence": 0.5,
                "kr_affinity_hint": 0.5,
            },
        ]
    )

    result, status = ai_analysis.apply_ai_adjudication(
        ranking, candidates, pd.DataFrame(), {"enabled": True}
    )

    assert status["success"] is True
    by_id = result.set_index("challenge_id")
    assert bool(by_id.loc["viral_news", "is_social_challenge"]) is False
    # A hard-filtered viral false positive keeps its penalized score for the
    # detail output but must rank after every accepted challenge.
    assert by_id.loc["weak_challenge", "final_rank"] < by_id.loc["viral_news", "final_rank"]


def test_backfilled_rejected_rows_stay_below_all_accepted_rows():
    ranking = pd.DataFrame(
        [
            {
                "challenge_id": "accepted_weak",
                "name": "약한 정식 챌린지",
                "final_rank": 1,
                "final_score": 6.0,
                "confidence": 0.4,
                "is_social_challenge": True,
                "representative_youtube_url": "https://youtu.be/AAAAAAAAAAA",
                "guide_youtube_url": "https://youtu.be/AAAAAAAAAAA",
                "instagram_posts_7d": 5,
                "entity_confidence": 0.9,
            },
            {
                "challenge_id": "rejected_viral",
                "name": "뉴스 영상",
                "final_rank": 2,
                "final_score": 9.0,
                "confidence": 0.9,
                "is_social_challenge": False,
                "representative_youtube_url": "https://youtu.be/BBBBBBBBBBB",
                "guide_youtube_url": "https://youtu.be/BBBBBBBBBBB",
                "instagram_posts_7d": 5,
                "entity_confidence": 0.9,
            },
        ]
    )
    config = {
        "ranking": {"top_n": 5, "exclude_ai_rejected": True, "backfill_to_top_n": True}
    }

    public, _ranked = _build_public_ranking(config, ranking)

    # The rejected row may only backfill BELOW accepted rows even when its
    # penalized score still exceeds a weak accepted challenge's score.
    assert list(public["id"]) == ["accepted_weak", "rejected_viral"]
    assert list(public["rank"]) == [1, 2]


def test_commentary_only_coverage_yields_no_representative_video() -> None:
    now = pd.Timestamp("2026-08-21T00:00:00Z")
    candidates = pd.DataFrame(
        [
            {
                "challenge_id": "c_news_only",
                "name": "새 춤 챌린지",
                "alias_list": ["새 춤 챌린지"],
            }
        ]
    )
    rows = pd.DataFrame(
        [
            {
                "challenge_id": "c_news_only",
                "platform": "youtube",
                "content_id": "NEWSVID1234",
                "youtube_url": "https://www.youtube.com/watch?v=NEWSVID1234",
                "title": "새 춤 챌린지 뉴스 정리",
                "caption": "새 춤 챌린지가 화제가 된 이유 분석",
                "hashtags": "새춤챌린지",
                "matched_alias": "새 춤 챌린지",
                "author_id": "news",
                "channel_title": "뉴스 채널",
                "created_at": now - pd.Timedelta(days=1),
                "views": 5_000_000,
                "likes": 100_000,
                "comments": 5_000,
                "kr_affinity": 0.9,
                "source_origin": "youtube_api",
            }
        ]
    )

    selected = select_representative_youtube(
        candidates, rows, now, {"enabled": True, "ai_participation_check": False}
    ).iloc[0]

    # When only news/commentary coverage exists the card must stay empty (null
    # link contract) instead of showing a news explainer as the representative.
    assert selected["representative_youtube_url"] == ""
    assert selected["guide_youtube_url"] == ""
