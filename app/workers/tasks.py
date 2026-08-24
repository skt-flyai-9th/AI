from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

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


@celery_app.task(name="app.workers.tasks.run_editing_pipeline", bind=True)
def run_editing_pipeline(self, run_id: str) -> dict:
    with SessionLocal() as db:
        run = db.get(EditingRun, run_id)
        if run is not None:
            request = getattr(self, "request", None)
            task_id = getattr(request, "id", None)
            if task_id:
                run.celery_task_id = task_id
                db.commit()
        completed = get_editing_agent_service().execute(db, run_id)
        return {"run_id": completed.id, "status": completed.status}


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
