from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.challenge import Challenge
from app.models.pipeline_run import PipelineRun
from app.models.ranking_snapshot import RankingSnapshot
from app.services.pipeline import (
    TrendResearchIncomplete,
    build_runtime_config,
    create_run,
    persist_result,
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


def _add_existing_ranking(db, *, count: int = 15, prefix: str = "existing") -> list[str]:
    now = datetime.now(timezone.utc)
    challenge_ids = []
    for rank in range(1, count + 1):
        challenge_id = f"{prefix}-{rank}"
        challenge_ids.append(challenge_id)
        db.add(
            Challenge(
                id=challenge_id,
                automatic_name=challenge_id,
                automatic_rank=rank,
                automatic_score=777.0,
                active=True,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    db.commit()
    return challenge_ids


def _add_run(db, run_id: str = "research-run") -> PipelineRun:
    run = PipelineRun(id=run_id, status="RUNNING", stage="PERSISTING", progress=85)
    db.add(run)
    db.commit()
    return run


def _valid_ranking(*, prefix: str, count: int = 100) -> pd.DataFrame:
    return pd.DataFrame(
        [
            _ranking_row(
                f"{prefix}-{index}",
                index,
                youtube_url=_youtube_url(index),
            )
            for index in range(1, count + 1)
        ]
    )


def test_runtime_config_restores_full_top_one_hundred_research() -> None:
    config = build_runtime_config()

    assert config["ranking"]["top_n"] == 100
    assert config["ranking"]["require_youtube_video"] is True
    assert "exclude_challenge_ids" not in config["ranking"]


def test_complete_batch_replaces_active_ranking_with_top_one_hundred() -> None:
    with SessionLocal() as db:
        old_ids = _add_existing_ranking(db)
        run = _add_run(db)

        persist_result(db, run, _valid_ranking(prefix="researched"), pd.DataFrame())

        active = list(
            db.scalars(
                select(Challenge)
                .where(Challenge.active.is_(True))
                .order_by(Challenge.automatic_rank)
            )
        )
        assert len(active) == 100
        assert [row.automatic_rank for row in active] == list(range(1, 101))
        assert {row.id for row in active} == {
            f"researched-{index}" for index in range(1, 101)
        }
        assert all(row.raw_details["research_auto_activated"] is True for row in active)
        assert all(db.get(Challenge, challenge_id).active is False for challenge_id in old_ids)

        snapshots = list(
            db.scalars(
                select(RankingSnapshot)
                .where(RankingSnapshot.run_id == run.id)
                .order_by(RankingSnapshot.automatic_rank)
            )
        )
        assert len(snapshots) == 100
        assert [row.automatic_rank for row in snapshots] == list(range(1, 101))


def test_persist_result_reuses_one_valid_youtube_url_for_both_contract_fields() -> None:
    with SessionLocal() as db:
        run = _add_run(db)
        rows = []
        for index in range(1, 3):
            row = _ranking_row(f"researched-{index}", index, youtube_url=_youtube_url(index))
            if index == 1:
                row["guide_youtube_url"] = ""
            rows.append(row)

        persist_result(
            db,
            run,
            pd.DataFrame(rows),
            pd.DataFrame(),
            expected_count=2,
        )

        stored = db.get(Challenge, "researched-1")
        assert stored is not None
        assert stored.automatic_representative_youtube_url == _youtube_url(1)
        assert stored.automatic_guide_youtube_url == _youtube_url(1)


def test_incomplete_batch_leaves_existing_database_untouched() -> None:
    with SessionLocal() as db:
        old_ids = _add_existing_ranking(db)
        run = _add_run(db)
        ranking = _valid_ranking(prefix="researched", count=99)

        with pytest.raises(TrendResearchIncomplete, match="found 99"):
            persist_result(db, run, ranking, pd.DataFrame())
        db.rollback()

        active_ids = list(
            db.scalars(
                select(Challenge.id)
                .where(Challenge.active.is_(True))
                .order_by(Challenge.automatic_rank)
            )
        )
        assert active_ids == old_ids
        assert list(db.scalars(select(RankingSnapshot))) == []


def test_complete_rerun_replaces_previous_top_one_hundred() -> None:
    with SessionLocal() as db:
        first_run = _add_run(db, "first-run")
        persist_result(db, first_run, _valid_ranking(prefix="first"), pd.DataFrame())

        second_run = _add_run(db, "second-run")
        persist_result(db, second_run, _valid_ranking(prefix="second"), pd.DataFrame())

        active_ids = set(db.scalars(select(Challenge.id).where(Challenge.active.is_(True))))
        inactive_first_ids = set(
            db.scalars(
                select(Challenge.id).where(
                    Challenge.id.like("first-%"), Challenge.active.is_(False)
                )
            )
        )
        assert active_ids == {f"second-{index}" for index in range(1, 101)}
        assert inactive_first_ids == {f"first-{index}" for index in range(1, 101)}


def test_create_run_always_allows_a_new_top_one_hundred_research() -> None:
    with SessionLocal() as db:
        previous = _add_run(db, "completed-run")
        previous.status = "COMPLETED"
        previous.stage = "COMPLETED"
        previous.finished_at = datetime.now(timezone.utc)
        db.commit()

        new_run = create_run(db)

        assert new_run.status == "QUEUED"
        assert new_run.id != previous.id
