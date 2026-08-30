from datetime import datetime, timezone

import pandas as pd

from app.db.session import SessionLocal
from app.models.challenge import Challenge
from app.models.pipeline_run import PipelineRun
from app.services.pipeline import persist_result


def _ranking_row(challenge_id: str, rank: int) -> dict:
    return {
        "challenge_id": challenge_id,
        "name": challenge_id,
        "final_rank": rank,
        "final_score": 90.0 - rank,
        "confidence": 0.9,
        "alias_list": [],
        "category": "test",
        "stage": "RISING",
        "kr_affinity": 0.8,
    }


def test_persist_result_keeps_only_approved_two_and_preserves_rank_gap() -> None:
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        approved_ids = (
            "jujutsu_transition",
            "otsukare_summer_challenge",
        )
        db.add_all(
            [
                Challenge(
                    id=challenge_id,
                    automatic_name=challenge_id,
                    automatic_rank=rank,
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                for rank, challenge_id in enumerate(approved_ids, start=1)
            ]
            + [
                Challenge(
                    id="legacy_discovered_trend",
                    automatic_name="삭제 대상",
                    automatic_rank=4,
                    active=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            ]
        )
        run = PipelineRun(id="prune-test", status="RUNNING", stage="PERSISTING", progress=85)
        db.add(run)
        db.commit()

        ranking = pd.DataFrame(
            [
                *[
                    _ranking_row(challenge_id, rank)
                    for rank, challenge_id in enumerate(approved_ids, 1)
                ],
                _ranking_row("new_unapproved_trend", 4),
            ]
        )
        persist_result(db, run, ranking, pd.DataFrame())

        assert {row.id for row in db.query(Challenge).all()} == set(approved_ids)
        assert {row.id: row.automatic_rank for row in db.query(Challenge).all()} == {
            "jujutsu_transition": 1,
            "otsukare_summer_challenge": 3,
        }
