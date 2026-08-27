from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.editing.service import (
    EditingAgentService,
    EditingDomainError,
    get_editing_agent_service,
    revision_response,
    validate_editing_runtime,
)
from app.core.security import require_internal_api_key
from app.core.config import get_settings
from app.db.session import get_db
from app.models.editing_run import EditingRun
from app.schemas.editing import (
    EditingRevisionRequest,
    EditingRevisionResponse,
    EditingRunCreateRequest,
    EditingRunCreateResponse,
    EditingRunRead,
    EditingRunResultResponse,
    EditingRunStatus,
)
from app.workers.tasks import enqueue_editing_pipeline

router = APIRouter(
    prefix="/editing-runs",
    tags=["editing"],
    dependencies=[Depends(require_internal_api_key)],
)


@router.post("", response_model=EditingRunCreateResponse, status_code=status.HTTP_202_ACCEPTED)
def create_editing_run(
    body: EditingRunCreateRequest,
    db: Session = Depends(get_db),
    service: EditingAgentService = Depends(get_editing_agent_service),
) -> EditingRunCreateResponse:
    _require_runtime()
    try:
        run = service.create_run(db, body)
    except EditingDomainError as exc:
        _raise_domain_error(exc)
    task = _enqueue_or_fail(db, service, run)
    run.celery_task_id = task.id
    db.commit()
    return EditingRunCreateResponse(
        run_id=run.id,
        status=EditingRunStatus(run.status),
        task_id=task.id,
    )


@router.get("/{run_id}", response_model=EditingRunRead)
def get_editing_run(run_id: str, db: Session = Depends(get_db)) -> EditingRunRead:
    run = db.get(EditingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Editing run not found")
    queue_position: int | None = None
    estimated_wait_sec: int | None = None
    if run.status == EditingRunStatus.QUEUED.value:
        ahead = db.scalar(
            select(func.count())
            .select_from(EditingRun)
            .where(
                EditingRun.status.in_(["RUNNING", "QUEUED"]),
                EditingRun.created_at < run.created_at,
            )
        ) or 0
        queue_position = int(ahead) + 1
        estimated_wait_sec = int(ahead) * get_settings().editing_estimated_seconds_per_run
    elif run.status == EditingRunStatus.RUNNING.value:
        queue_position = 0
        estimated_wait_sec = 0

    started = run.stage_started_at or run.started_at or run.created_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    ended = run.finished_at or datetime.now(timezone.utc)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    stage_elapsed_sec = max(0, int((ended - started).total_seconds()))
    return EditingRunRead.model_validate(run).model_copy(
        update={
            "queue_position": queue_position,
            "estimated_wait_sec": estimated_wait_sec,
            "stage_elapsed_sec": stage_elapsed_sec,
        }
    )


@router.get("/{run_id}/result", response_model=EditingRunResultResponse)
def get_editing_result(
    run_id: str,
    db: Session = Depends(get_db),
    service: EditingAgentService = Depends(get_editing_agent_service),
) -> EditingRunResultResponse:
    run = db.get(EditingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Editing run not found")
    if run.status not in {
        EditingRunStatus.COMPLETED.value,
        EditingRunStatus.SOURCE_GAP.value,
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "Editing result is not available.",
                "status": run.status,
                "stage": run.stage,
                "progress": run.progress,
                "error_message": run.error_message if run.status == "FAILED" else None,
            },
        )
    return service.result(run)


@router.post(
    "/{run_id}/revisions",
    response_model=EditingRevisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def revise_editing_run(
    run_id: str,
    body: EditingRevisionRequest,
    db: Session = Depends(get_db),
    service: EditingAgentService = Depends(get_editing_agent_service),
) -> EditingRevisionResponse:
    _require_runtime()
    try:
        revision = service.create_revision(db, run_id, body)
    except EditingDomainError as exc:
        _raise_domain_error(exc)
    task = _enqueue_or_fail(db, service, revision)
    revision.celery_task_id = task.id
    db.commit()
    return revision_response(revision)


def _require_runtime() -> None:
    runtime = validate_editing_runtime()
    missing = [name for name, ready in runtime.items() if not ready]
    if missing:
        raise HTTPException(status_code=422, detail={"missing_editing_dependencies": missing})


def _enqueue_or_fail(
    db: Session,
    service: EditingAgentService,
    run: EditingRun,
):
    try:
        return enqueue_editing_pipeline(run.id)
    except Exception as exc:
        service.mark_enqueue_failed(db, run)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "EDITING_TASK_ENQUEUE_FAILED",
                "message": "The editing task could not be queued. Retry the request.",
                "run_id": run.id,
            },
        ) from exc


def _raise_domain_error(exc: EditingDomainError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc
