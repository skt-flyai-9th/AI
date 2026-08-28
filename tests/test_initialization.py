from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.pipeline_run import PipelineRun
from app.services.initialization import initialize_service_once
from app.workers.celery_app import celery_app


def _complete_ranking(db, run_id: str) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    assert run is not None
    run.status = "COMPLETED"
    run.stage = "COMPLETED"
    run.progress = 100
    run.started_at = datetime.now(timezone.utc)
    run.finished_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run


def test_initializer_runs_ranking_only_before_the_first_success():
    calls: list[str] = []

    def executor(db, run_id: str) -> PipelineRun:
        calls.append(run_id)
        return _complete_ranking(db, run_id)

    with SessionLocal() as db:
        first = initialize_service_once(db, ranking_executor=executor)
        second = initialize_service_once(db, ranking_executor=executor)

        assert first["mode"] == "INITIAL_ONCE"
        assert first["ranking"]["executed"] is True
        assert second["ranking"]["executed"] is False
        assert second["ranking"]["run_id"] == first["ranking"]["run_id"]
        assert len(calls) == 1
        assert len(list(db.scalars(select(PipelineRun)))) == 1


def test_celery_beat_only_schedules_operational_editing_recovery():
    schedules = celery_app.conf.beat_schedule

    assert set(schedules) == {"recover-orphaned-editing-runs"}
    assert schedules["recover-orphaned-editing-runs"]["schedule"] == 300
    assert schedules["recover-orphaned-editing-runs"]["task"] == (
        "app.workers.tasks.recover_orphaned_editing_runs"
    )


def test_compose_uses_one_shot_initializer_and_recovery_only_beat():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert services["beat"]["command"].endswith("beat --loglevel=INFO")
    assert services["initializer"]["command"] == "ai-service initialize-once"
    assert services["initializer"]["restart"] == "no"
    assert services["api"]["depends_on"]["initializer"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["worker"]["depends_on"]["initializer"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["beat"]["depends_on"]["initializer"]["condition"] == (
        "service_completed_successfully"
    )
