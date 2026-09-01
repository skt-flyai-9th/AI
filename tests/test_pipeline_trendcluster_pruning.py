from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from app.agents.challenge_ranking.trendcluster import PINNED_TREND_RANKS
from app.db.session import SessionLocal
from app.models.challenge import Challenge
from app.models.pipeline_run import PipelineRun
from app.models.ranking_snapshot import RankingSnapshot
from app.services.pipeline import (
    TrendExpansionAlreadyComplete,
    TrendExpansionIncomplete,
    create_run,
    persist_result,
    trend_expansion_complete,
)


def _youtube_url(index: int) -> str:
    return f"https://www.youtube.com/watch?v=N{index:010d}"


def _ranking_row(
    challenge_id: str,
    rank: int,
    *,
    youtube_url: str | None = None,
    is_social_challenge: bool = True,
) -> dict:
    return {
        "challenge_id": challenge_id,
        "name": challenge_id,
        "final_rank": rank,
        "final_score": 100.0 - rank,
        "confidence": 0.9,
        "alias_list": [],
        "category": "test",
        "stage": "RISING",
        "kr_affinity": 0.8,
        "is_social_challenge": is_social_challenge,
        "representative_youtube_url": youtube_url or "",
        "guide_youtube_url": youtube_url or "",
    }


def _add_pinned_rows(db) -> dict[str, tuple[str, int]]:
    now = datetime.now(timezone.utc)
    original: dict[str, tuple[str, int]] = {}
    for challenge_id, rank in PINNED_TREND_RANKS.items():
        name = f"pinned-{challenge_id}"
        original[challenge_id] = (name, rank)
        db.add(
            Challenge(
                id=challenge_id,
                automatic_name=name,
                automatic_rank=rank,
                automatic_score=777.0,
                active=True,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    db.commit()
    return original


def _add_run(db, run_id: str = "research-run") -> PipelineRun:
    run = PipelineRun(id=run_id, status="RUNNING", stage="PERSISTING", progress=85)
    db.add(run)
    db.commit()
    return run


def test_persist_result_appends_eleven_url_backed_trends_without_changing_top_four() -> None:
    with SessionLocal() as db:
        original = _add_pinned_rows(db)
        run = _add_run(db)
        ranking = pd.DataFrame(
            [
                _ranking_row("missing-url", 1),
                _ranking_row(
                    "not-a-challenge",
                    2,
                    youtube_url=_youtube_url(99),
                    is_social_challenge=False,
                ),
                *[
                    _ranking_row(
                        f"researched-{index}",
                        index + 2,
                        youtube_url=_youtube_url(index),
                    )
                    for index in range(1, 12)
                ],
            ]
        )

        persist_result(db, run, ranking, pd.DataFrame())

        active = list(
            db.scalars(
                select(Challenge)
                .where(Challenge.active.is_(True))
                .order_by(Challenge.automatic_rank)
            )
        )
        assert len(active) == 15
        assert [(row.id, row.automatic_rank) for row in active[:4]] == [
            (challenge_id, rank) for challenge_id, rank in PINNED_TREND_RANKS.items()
        ]
        assert [(row.automatic_name, row.automatic_rank) for row in active[:4]] == [
            (original[challenge_id][0], rank) for challenge_id, rank in PINNED_TREND_RANKS.items()
        ]
        assert [row.automatic_rank for row in active[4:]] == list(range(5, 16))
        assert {row.id for row in active[4:]} == {f"researched-{index}" for index in range(1, 12)}
        assert all(row.raw_details["research_auto_activated"] is True for row in active[4:])
        snapshots = list(
            db.scalars(
                select(RankingSnapshot)
                .where(RankingSnapshot.run_id == run.id)
                .order_by(RankingSnapshot.automatic_rank)
            )
        )
        assert [row.automatic_rank for row in snapshots] == list(range(5, 16))
        assert trend_expansion_complete(db) is True


def test_persist_result_reuses_one_valid_youtube_url_for_both_contract_fields() -> None:
    with SessionLocal() as db:
        _add_pinned_rows(db)
        run = _add_run(db)
        rows = []
        for index in range(1, 12):
            row = _ranking_row(f"researched-{index}", index, youtube_url=_youtube_url(index))
            if index == 1:
                row["guide_youtube_url"] = ""
            rows.append(row)

        persist_result(db, run, pd.DataFrame(rows), pd.DataFrame())

        stored = db.get(Challenge, "researched-1")
        assert stored is not None
        assert stored.automatic_representative_youtube_url == _youtube_url(1)
        assert stored.automatic_guide_youtube_url == _youtube_url(1)


def test_persist_result_is_all_or_nothing_when_fewer_than_eleven_urls_exist() -> None:
    with SessionLocal() as db:
        original = _add_pinned_rows(db)
        run = _add_run(db)
        ranking = pd.DataFrame(
            [
                _ranking_row(f"researched-{index}", index, youtube_url=_youtube_url(index))
                for index in range(1, 11)
            ]
        )

        with pytest.raises(TrendExpansionIncomplete, match="found 10"):
            persist_result(db, run, ranking, pd.DataFrame())
        db.rollback()

        rows = list(db.scalars(select(Challenge).order_by(Challenge.automatic_rank)))
        assert [(row.id, row.automatic_name, row.automatic_rank) for row in rows] == [
            (challenge_id, original[challenge_id][0], rank)
            for challenge_id, rank in PINNED_TREND_RANKS.items()
        ]
        assert list(db.scalars(select(RankingSnapshot))) == []


def test_create_run_refuses_to_duplicate_a_completed_expansion() -> None:
    with SessionLocal() as db:
        _add_pinned_rows(db)
        first_run = _add_run(db)
        ranking = pd.DataFrame(
            [
                _ranking_row(f"researched-{index}", index, youtube_url=_youtube_url(index))
                for index in range(1, 12)
            ]
        )
        persist_result(db, first_run, ranking, pd.DataFrame())

        with pytest.raises(TrendExpansionAlreadyComplete):
            create_run(db)


def test_create_run_allows_explicit_replacement_of_a_completed_expansion() -> None:
    with SessionLocal() as db:
        _add_pinned_rows(db)
        first_run = _add_run(db)
        ranking = pd.DataFrame(
            [
                _ranking_row(f"researched-{index}", index, youtube_url=_youtube_url(index))
                for index in range(1, 12)
            ]
        )
        persist_result(db, first_run, ranking, pd.DataFrame())

        replacement_run = create_run(db, replace_expansion=True)

        assert replacement_run.status == "QUEUED"
