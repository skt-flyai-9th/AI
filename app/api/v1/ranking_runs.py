from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.core.security import require_admin_token
from app.db.session import get_db
from app.models.pipeline_run import PipelineRun
from app.schemas.pipeline_run import PipelineRunCreateResponse, PipelineRunRead
from app.services.pipeline import create_run, validate_runtime_keys
from app.workers.tasks import enqueue_ranking_pipeline

router = APIRouter(prefix="/ranking-runs", tags=["ranking-runs"])


@router.post(
    "",
    response_model=PipelineRunCreateResponse,
    dependencies=[Depends(require_admin_token)],
)
def start_ranking_run(db: Session = Depends(get_db)) -> PipelineRunCreateResponse:
    key_status = validate_runtime_keys()
    missing = [name for name, ready in key_status.items() if not ready]
    if missing:
        raise HTTPException(status_code=422, detail={"missing_api_keys": missing})
    run = create_run(db)
    task = enqueue_ranking_pipeline(run.id)
    run.celery_task_id = task.id
    db.commit()
    return PipelineRunCreateResponse(run_id=run.id, status=run.status, task_id=task.id)


@router.get("/latest", response_model=PipelineRunRead)
def latest_run(db: Session = Depends(get_db)) -> PipelineRun:
    run = db.scalar(select(PipelineRun).order_by(desc(PipelineRun.created_at)).limit(1))
    if run is None:
        raise HTTPException(status_code=404, detail="No ranking run found")
    return run


@router.get("/{run_id}", response_model=PipelineRunRead)
def get_run(run_id: str, db: Session = Depends(get_db)) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Ranking run not found")
    return run
