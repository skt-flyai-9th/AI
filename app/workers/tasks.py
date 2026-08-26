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
)
def run_editing_pipeline(self, run_id: str) -> dict:
    with SessionLocal() as db:
        run = db.get(EditingRun, run_id)
        if run is not None:
            request = getattr(self, "request", None)
            task_id = getattr(request, "id", None)
            delivery_info = getattr(request, "delivery_info", {}) or {}
            if run.status == "RUNNING" and delivery_info.get("redelivered"):
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

    control = getattr(celery_app, "control", None)
    if control is None:
        return {"status": "inspector_unavailable", "requeued": []}
    try:
        active_by_worker = control.inspect(timeout=2.0).active()
    except Exception:
        return {"status": "inspector_unavailable", "requeued": []}
    if not active_by_worker:
        return {"status": "inspector_unavailable", "requeued": []}

    active_task_ids = {
        str(task["id"])
        for tasks in active_by_worker.values()
        for task in (tasks or [])
        if task.get("id")
    }
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=settings.editing_orphan_stale_seconds
    )
    recovered_ids: list[str] = []
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
    return {"status": "ok", "requeued": requeued, "failed": failed}


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

    task_id = str(uuid4())

    class _Request:
        id = task_id

    class _TaskSelf:
        request = _Request()

    run_editing_pipeline(_TaskSelf(), run_id)
    return _ImmediateResult(id=task_id)


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
