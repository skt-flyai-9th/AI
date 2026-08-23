from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.core.config import Settings
from app.db.session import SessionLocal
from app.models.challenge import Challenge
from app.models.pipeline_run import PipelineRun
from app.models.ranking_snapshot import RankingSnapshot
from app.ranker_core.db import initialize_database, prune_run_history
from app.services.retention import cleanup_history


def _settings(tmp_path) -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite:///./runtime-data/test.db",
        ranker_data_dir=tmp_path,
        history_cleanup_enabled=True,
        run_retention_days=90,
        failed_run_retention_days=14,
        min_successful_runs_to_keep=10,
    )


def test_cleanup_history_bounds_postgres_snapshots(tmp_path):
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        challenge = Challenge(
            id="retention-test",
            automatic_name="Retention Test",
            active=True,
            last_seen_at=now,
        )
        db.add(challenge)

        for index in range(12):
            finished_at = now - timedelta(days=120 + index)
            run = PipelineRun(
                id=f"completed-{index}",
                status="COMPLETED",
                stage="COMPLETED",
                progress=100,
                created_at=finished_at,
                started_at=finished_at,
                finished_at=finished_at,
            )
            db.add(run)
            db.flush()
            db.add(
                RankingSnapshot(
                    run_id=run.id,
                    challenge_id=challenge.id,
                    automatic_rank=1,
                    automatic_score=90.0,
                    row_data={"name": "Retention Test"},
                    source_metrics={},
                    created_at=finished_at,
                )
            )

        old_failed_at = now - timedelta(days=30)
        recent_failed_at = now - timedelta(days=2)
        db.add_all(
            [
                PipelineRun(
                    id="failed-old",
                    status="FAILED",
                    stage="FAILED",
                    progress=20,
                    created_at=old_failed_at,
                    finished_at=old_failed_at,
                ),
                PipelineRun(
                    id="failed-recent",
                    status="FAILED",
                    stage="FAILED",
                    progress=20,
                    created_at=recent_failed_at,
                    finished_at=recent_failed_at,
                ),
            ]
        )
        db.commit()

        result = cleanup_history(db, now=now, settings=_settings(tmp_path))

        completed_count = int(
            db.scalar(
                select(func.count(PipelineRun.id)).where(
                    PipelineRun.status == "COMPLETED"
                )
            )
            or 0
        )
        snapshot_count = int(db.scalar(select(func.count(RankingSnapshot.id))) or 0)

        assert result["postgres"]["deleted_runs"] == 3
        assert result["postgres"]["deleted_snapshots"] == 2
        assert completed_count == 10
        assert snapshot_count == 10
        assert db.get(PipelineRun, "failed-old") is None
        assert db.get(PipelineRun, "failed-recent") is not None


def test_prune_legacy_sqlite_history_keeps_minimum_runs(tmp_path):
    path = tmp_path / "ranker-history.sqlite3"
    now = datetime.now(timezone.utc)
    connection = initialize_database(path)
    try:
        with connection:
            for index in range(12):
                run_id = f"run-{index}"
                run_at = now - timedelta(days=120 + index)
                connection.execute(
                    "INSERT INTO runs(run_id, run_at, statuses_json, config_json) VALUES (?, ?, ?, ?)",
                    (run_id, run_at.isoformat(), "{}", "{}"),
                )
                connection.execute(
                    "INSERT INTO rankings(run_id, challenge_id, final_rank, final_score, row_json) VALUES (?, ?, ?, ?, ?)",
                    (run_id, "challenge", 1, 90.0, "{}"),
                )
    finally:
        connection.close()

    result = prune_run_history(
        path,
        retention_days=90,
        min_runs_to_keep=10,
        now=now,
    )

    connection = initialize_database(path)
    try:
        runs = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        rankings = int(connection.execute("SELECT COUNT(*) FROM rankings").fetchone()[0])
    finally:
        connection.close()

    assert result["deleted_runs"] == 2
    assert result["remaining_runs"] == 10
    assert result["vacuumed"] is True
    assert runs == 10
    assert rankings == 10
