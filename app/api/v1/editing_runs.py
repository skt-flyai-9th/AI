from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.editing.service import (
    EditingAgentService,
    EditingDomainError,
    get_editing_agent_service,
    revision_response,
    validate_editing_runtime,
)
from app.core.security import require_internal_api_key
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
    task = enqueue_editing_pipeline(run.id)
    run.celery_task_id = task.id
    db.commit()
    return EditingRunCreateResponse(
        run_id=run.id,
        status=EditingRunStatus(run.status),
        task_id=task.id,
    )


@router.get("/{run_id}", response_model=EditingRunRead)
def get_editing_run(run_id: str, db: Session = Depends(get_db)) -> EditingRun:
    run = db.get(EditingRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Editing run not found")
    return run


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
    task = enqueue_editing_pipeline(revision.id)
    revision.celery_task_id = task.id
    db.commit()
    return revision_response(revision)


def _require_runtime() -> None:
    runtime = validate_editing_runtime()
    missing = [name for name, ready in runtime.items() if not ready]
    if missing:
        raise HTTPException(status_code=422, detail={"missing_editing_dependencies": missing})


def _raise_domain_error(exc: EditingDomainError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    ) from exc
