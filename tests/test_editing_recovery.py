from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.db.session import SessionLocal
from app.models.editing_run import EditingRun
from app.workers import tasks


class _Inspector:
    def __init__(self, active):
        self._active = active

    def active(self):
        return self._active


class _Control:
    def __init__(self, active):
        self._active = active

    def inspect(self, **_kwargs):
        return _Inspector(self._active)


def _running(run_id: str, task_id: str) -> EditingRun:
    return EditingRun(
        id=run_id,
        status="RUNNING",
        stage="PLANNING_RECIPE",
        progress=35,
        celery_task_id=task_id,
        request_snapshot={},
        video_context=[],
        warnings=[],
        missing_scene_roles=[],
        available_options=[],
        started_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )


def test_recovery_requeues_only_stale_runs_absent_from_active_workers(monkeypatch):
    with SessionLocal() as db:
        db.add_all(
            [
                _running("edit_orphan", "task-orphan"),
                _running("edit_active", "task-active"),
            ]
        )
        db.commit()

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(
            editing_orphan_recovery_enabled=True,
            editing_orphan_stale_seconds=60,
        ),
    )
    monkeypatch.setattr(tasks.celery_app, "control", _Control({"worker": [{"id": "task-active"}]}))
    monkeypatch.setattr(
        tasks,
        "enqueue_editing_pipeline",
        lambda _run_id: tasks._ImmediateResult(id="task-requeued"),
    )

    result = tasks.recover_orphaned_editing_runs.run()

    assert result == {
        "status": "ok",
        "requeued": ["edit_orphan"],
        "failed": [],
        "recovery_exhausted": [],
    }
    with SessionLocal() as db:
        orphan = db.get(EditingRun, "edit_orphan")
        active = db.get(EditingRun, "edit_active")
        assert orphan.status == "QUEUED"
        assert orphan.stage == "QUEUED"
        assert orphan.progress == 0
        assert orphan.started_at is None
        assert orphan.celery_task_id == "task-requeued"
        assert active.status == "RUNNING"
        assert active.celery_task_id == "task-active"


def test_recovery_fails_closed_when_no_worker_answers(monkeypatch):
    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(editing_orphan_recovery_enabled=True),
    )
    monkeypatch.setattr(tasks.celery_app, "control", _Control(None))

    assert tasks.recover_orphaned_editing_runs.run() == {
        "status": "inspector_unavailable",
        "requeued": [],
    }


def test_recovery_marks_run_failed_after_bounded_attempts(monkeypatch):
    with SessionLocal() as db:
        run = _running("edit_exhausted", "task-old")
        run.recovery_attempts = 2
        db.add(run)
        db.commit()

    monkeypatch.setattr(
        tasks,
        "get_settings",
        lambda: Settings(
            editing_orphan_recovery_enabled=True,
            editing_orphan_stale_seconds=60,
            editing_orphan_max_recovery_attempts=2,
        ),
    )
    monkeypatch.setattr(tasks.celery_app, "control", _Control({"worker": []}))

    result = tasks.recover_orphaned_editing_runs.run()

    assert result["recovery_exhausted"] == ["edit_exhausted"]
    with SessionLocal() as db:
        run = db.get(EditingRun, "edit_exhausted")
        assert run.status == "FAILED"
        assert "RECOVERY_EXHAUSTED" in run.error_message


def test_redelivered_task_resumes_a_run_left_running(monkeypatch):
    with SessionLocal() as db:
        db.add(_running("edit_redelivered", "task-old"))
        db.commit()

    observed_statuses: list[str] = []

    class _Service:
        def execute(self, db, run_id):
            run = db.get(EditingRun, run_id)
            observed_statuses.append(run.status)
            return run

    monkeypatch.setattr(tasks, "get_editing_agent_service", lambda: _Service())
    tasks.run_editing_pipeline.push_request(
        id="task-redelivered",
        delivery_info={"redelivered": True},
    )
    try:
        result = tasks.run_editing_pipeline.run("edit_redelivered")
    finally:
        tasks.run_editing_pipeline.pop_request()

    assert observed_statuses == ["QUEUED"]
    assert result == {"run_id": "edit_redelivered", "status": "QUEUED"}
    with SessionLocal() as db:
        run = db.get(EditingRun, "edit_redelivered")
        assert run.celery_task_id == "task-redelivered"


def test_celery_uses_late_ack_and_single_task_prefetch():
    assert tasks.run_editing_pipeline.acks_late is True
    assert tasks.run_editing_pipeline.reject_on_worker_lost is True
    assert tasks.celery_app.conf.worker_prefetch_multiplier == 1
