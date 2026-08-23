from datetime import datetime, timezone

from app.db.session import SessionLocal
from app.models.challenge import Challenge
from app.models.pipeline_run import PipelineRun
from app.models.ranking_snapshot import RankingSnapshot


def seed_run(status: str) -> str:
    run_id = f"run-{status.lower()}"
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        db.add(
            Challenge(
                id="bad-challenge",
                automatic_name="BAD 챌린지",
                automatic_rank=1,
                automatic_score=91.2,
                lifecycle="RISING",
                kr_affinity=0.9,
                confidence=0.8,
                category="dance",
                active=True,
                last_seen_at=now,
            )
        )
        run = PipelineRun(
            id=run_id,
            status=status,
            stage=status,
            progress=100 if status == "COMPLETED" else 40,
            finished_at=now if status == "COMPLETED" else None,
        )
        db.add(run)
        db.flush()

        if status == "COMPLETED":
            db.add(
                RankingSnapshot(
                    run_id=run_id,
                    challenge_id="bad-challenge",
                    automatic_rank=1,
                    automatic_score=91.2,
                    row_data={
                        "name": "BAD 챌린지",
                        "representative_youtube_url": "https://www.youtube.com/watch?v=AAA",
                        "guide_youtube_url": "https://www.youtube.com/watch?v=BBB",
                    },
                    source_metrics={},
                )
            )
        db.commit()

    return run_id


def test_backend_can_fetch_immutable_run_result(client, auth_headers):
    run_id = seed_run("COMPLETED")
    response = client.get(f"/api/v1/ranking-runs/{run_id}/result", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert payload["count"] == 1
    assert payload["results"][0] == {
        "id": "bad-challenge",
        "rank": 1,
        "name": "BAD 챌린지",
        "representative_youtube_url": "https://www.youtube.com/watch?v=AAA",
        "guide_youtube_url": "https://www.youtube.com/watch?v=BBB",
    }


def test_run_result_returns_conflict_while_processing(client, auth_headers):
    run_id = seed_run("RUNNING")
    response = client.get(f"/api/v1/ranking-runs/{run_id}/result", headers=auth_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "RUNNING"
