from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select

from app.agents.challenge_ranking.service import create_run, execute_pipeline
from app.agents.editing.service import get_editing_agent_service
from app.db.session import SessionLocal
from app.models.pipeline_run import PipelineRun
from app.models.editing_run import EditingRun
from app.models.template_knowledge_run import TemplateKnowledgeRun
from app.services.retention import cleanup_history
from app.template_knowledge.maintenance import run_scheduled_template_maintenance
from app.template_knowledge.service import get_template_knowledge_service
from app.workers.celery_app import celery_app
from app.core.config import get_settings


@dataclass
class _ImmediateResult:
    id: str


def _active_task_id_counts() -> dict[str, int] | None:
    """Count actively executing task instances per id, or None when inspection fails."""
    control = getattr(celery_app, "control", None)
    if control is None:
        return None
    try:
        active_by_worker = control.inspect(timeout=2.0).active()
    except Exception:
        return None
    if not active_by_worker:
        return None
    counts: dict[str, int] = {}
    for tasks in active_by_worker.values():
        for task in tasks or []:
            key = str(task.get("id") or "")
            if key:
                counts[key] = counts.get(key, 0) + 1
    return counts


def _editing_run_execution_alive(run: EditingRun, task_id: str | None) -> bool:
    """Detect whether another live execution of this run exists.

    A broker redelivery keeps the original task id, so when this redelivered
    copy is executing, the id it shares with the original counts twice in the
    active-task inspection. A differing recorded id (e.g. after a beat requeue)
    is alive when it appears at all. When inspection is unavailable we assume
    the original is dead, preserving the bounded recovery behaviour.
    """
    counts = _active_task_id_counts()
    if counts is None:
        return False
    if run.celery_task_id and run.celery_task_id != task_id:
        return counts.get(run.celery_task_id, 0) > 0
    if task_id:
        return counts.get(task_id, 0) > 1
    return False


@celery_app.task(name="app.workers.tasks.run_ranking_pipeline", bind=True)
def run_ranking_pipeline(self, run_id: str | None = None) -> dict:
    with SessionLocal() as db:
        if run_id is None:
            run = create_run(db)
            run_id = run.id
        run = db.get(PipelineRun, run_id)
        if run is not None:
            request = getattr(self, "request", None)
            task_id = getattr(request, "id", None)
            if task_id:
                run.celery_task_id = task_id
                db.commit()
        completed = execute_pipeline(db, run_id)
        return {"run_id": completed.id, "status": completed.status}


@celery_app.task(name="app.workers.tasks.cleanup_history")
def cleanup_history_task() -> dict:
    with SessionLocal() as db:
        return cleanup_history(db)


@celery_app.task(name="app.workers.tasks.run_database_maintenance")
def run_database_maintenance_task() -> dict:
    with SessionLocal() as db:
        return run_scheduled_template_maintenance(db)


@celery_app.task(name="app.workers.tasks.run_database_knowledge", bind=True)
def run_database_knowledge(self, run_id: str) -> dict:
    with SessionLocal() as db:
        run = db.get(TemplateKnowledgeRun, run_id)
        if run is not None:
            request = getattr(self, "request", None)
            task_id = getattr(request, "id", None)
            if task_id:
                run.celery_task_id = task_id
                db.commit()
        completed = get_template_knowledge_service().execute_run(db, run_id)
        return {"run_id": completed.id, "status": completed.status}


@celery_app.task(
    name="app.workers.tasks.run_editing_pipeline",
    bind=True,
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=max(60, get_settings().editing_task_timeout_seconds - 30),
    time_limit=get_settings().editing_task_timeout_seconds,
)
def run_editing_pipeline(self, run_id: str) -> dict:
    with SessionLocal() as db:
        run = db.get(EditingRun, run_id)
        if run is not None:
            request = getattr(self, "request", None)
            task_id = getattr(request, "id", None)
            delivery_info = getattr(request, "delivery_info", {}) or {}
            if run.status == "RUNNING" and delivery_info.get("redelivered"):
                if _editing_run_execution_alive(run, task_id):
                    # The original execution is still running (e.g. a broker
                    # visibility-timeout redelivery); executing this copy too
                    # would double-render and race commits on the same run.
                    return {
                        "run_id": run.id,
                        "status": run.status,
                        "skipped": "EXECUTION_STILL_ACTIVE",
                    }
                run.recovery_attempts = int(run.recovery_attempts or 0) + 1
                if run.recovery_attempts > get_settings().editing_orphan_max_recovery_attempts:
                    run.status = "FAILED"
                    run.stage = "FAILED"
                    run.error_message = "EDITING_RECOVERY_EXHAUSTED: Worker redelivery limit exceeded."
                    run.finished_at = datetime.now(timezone.utc)
                    db.commit()
                    return {"run_id": run.id, "status": run.status}
                run.status = "QUEUED"
                run.stage = "QUEUED"
                run.progress = 0
                run.started_at = None
                run.finished_at = None
                run.error_message = None
            if task_id:
                run.celery_task_id = task_id
                db.commit()
        completed = get_editing_agent_service().execute(db, run_id)
        return {"run_id": completed.id, "status": completed.status}


@celery_app.task(name="app.workers.tasks.recover_orphaned_editing_runs")
def recover_orphaned_editing_runs() -> dict:
    settings = get_settings()
    if not settings.editing_orphan_recovery_enabled:
        return {"status": "disabled", "requeued": []}

    active_counts = _active_task_id_counts()
    if active_counts is None:
        return {"status": "inspector_unavailable", "requeued": []}
    active_task_ids = set(active_counts)
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.editing_orphan_stale_seconds
    )
    recovered_ids: list[str] = []
    exhausted_ids: list[str] = []
    with SessionLocal() as db:
        statement = (
            select(EditingRun)
            .where(
                EditingRun.status == "RUNNING",
                EditingRun.started_at.is_not(None),
                EditingRun.started_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        for run in db.scalars(statement).all():
            if run.celery_task_id and run.celery_task_id in active_task_ids:
                continue
            run.recovery_attempts = int(run.recovery_attempts or 0) + 1
            if run.recovery_attempts > settings.editing_orphan_max_recovery_attempts:
                run.status = "FAILED"
                run.stage = "FAILED"
                run.error_message = "EDITING_RECOVERY_EXHAUSTED: Orphan recovery limit exceeded."
                run.finished_at = datetime.now(timezone.utc)
                exhausted_ids.append(run.id)
                continue
            run.status = "QUEUED"
            run.stage = "QUEUED"
            run.progress = 0
            run.celery_task_id = None
            run.started_at = None
            run.finished_at = None
            run.error_message = None
            recovered_ids.append(run.id)
        db.commit()

    requeued: list[str] = []
    failed: list[str] = []
    for run_id in recovered_ids:
        try:
            task = enqueue_editing_pipeline(run_id)
            with SessionLocal() as db:
                run = db.get(EditingRun, run_id)
                if run is not None and run.status == "QUEUED":
                    run.celery_task_id = task.id
                    db.commit()
            requeued.append(run_id)
        except Exception:
            with SessionLocal() as db:
                run = db.get(EditingRun, run_id)
                if run is not None:
                    get_editing_agent_service().mark_enqueue_failed(db, run)
            failed.append(run_id)
    return {
        "status": "ok",
        "requeued": requeued,
        "failed": failed,
        "recovery_exhausted": exhausted_ids,
    }


def enqueue_ranking_pipeline(run_id: str):
    delay = getattr(run_ranking_pipeline, "delay", None)
    if callable(delay):
        return delay(run_id)

    task_id = str(uuid4())

    class _Request:
        id = task_id

    class _TaskSelf:
        request = _Request()

    run_ranking_pipeline(_TaskSelf(), run_id)
    return _ImmediateResult(id=task_id)


def enqueue_editing_pipeline(run_id: str):
    delay = getattr(run_editing_pipeline, "delay", None)
    if callable(delay):
        return delay(run_id)

    raise RuntimeError("Celery is unavailable; editing tasks cannot run inline.")


def enqueue_database_knowledge(run_id: str):
    delay = getattr(run_database_knowledge, "delay", None)
    if callable(delay):
        return delay(run_id)

    task_id = str(uuid4())

    class _Request:
        id = task_id

    class _TaskSelf:
        request = _Request()

    run_database_knowledge(_TaskSelf(), run_id)
    return _ImmediateResult(id=task_id)
