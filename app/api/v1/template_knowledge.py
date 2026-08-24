from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import require_internal_api_key
from app.db.session import get_db
from app.schemas.template_knowledge import (
    CandidateDecision,
    CandidateRejection,
    EditingCandidateCreate,
    TemplateCandidateRead,
    TemplateCandidateStatus,
    TemplateKnowledgeOperation,
    TemplateKnowledgeRunCreateResponse,
    TemplateKnowledgeRunRead,
    TemplateKnowledgeRunResult,
    TemplateKnowledgeRunStatus,
    TemplateType,
    TemplateVersionRead,
    TemplateVersionStatus,
    TradeAreaAnalyzeRequest,
    TradeAreaCandidateCreate,
)
from app.template_knowledge.seeds import seed_template_library
from app.template_knowledge.service import (
    TemplateKnowledgeDomainError,
    TemplateKnowledgeService,
    get_template_knowledge_service,
)
from app.workers.tasks import enqueue_template_knowledge

router = APIRouter(
    prefix="/template-knowledge",
    tags=["template-knowledge"],
    dependencies=[Depends(require_internal_api_key)],
)

Service = Annotated[TemplateKnowledgeService, Depends(get_template_knowledge_service)]


@router.get("/candidates", response_model=list[TemplateCandidateRead])
def list_candidates(
    template_type: TemplateType | None = None,
    candidate_status: TemplateCandidateStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    service: Service = None,
) -> list[TemplateCandidateRead]:
    return service.list_candidates(
        db, template_type=template_type, status=candidate_status, limit=limit
    )


@router.get("/candidates/{candidate_id}", response_model=TemplateCandidateRead)
def get_candidate(
    candidate_id: str,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateCandidateRead:
    try:
        return TemplateCandidateRead.model_validate(service.get_candidate(db, candidate_id))
    except TemplateKnowledgeDomainError as exc:
        raise _http_error(exc) from exc


@router.post(
    "/trade-area/candidates",
    response_model=TemplateKnowledgeRunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_trade_area_candidate(
    request: TradeAreaCandidateCreate,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateKnowledgeRunCreateResponse:
    _require_runtime(TemplateKnowledgeOperation.TRADE_AREA_CANDIDATE)
    return _start_run(
        db,
        service,
        TemplateKnowledgeOperation.TRADE_AREA_CANDIDATE,
        request.model_dump(mode="json"),
    )


@router.post(
    "/video-editing/candidates",
    response_model=TemplateKnowledgeRunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_editing_candidate(
    request: EditingCandidateCreate,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateKnowledgeRunCreateResponse:
    _require_runtime(TemplateKnowledgeOperation.VIDEO_EDITING_CANDIDATE)
    return _start_run(
        db,
        service,
        TemplateKnowledgeOperation.VIDEO_EDITING_CANDIDATE,
        request.model_dump(mode="json"),
    )


@router.post("/candidates/{candidate_id}/validate", response_model=TemplateCandidateRead)
def validate_candidate(
    candidate_id: str,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateCandidateRead:
    try:
        return TemplateCandidateRead.model_validate(service.validate_candidate(db, candidate_id))
    except TemplateKnowledgeDomainError as exc:
        raise _http_error(exc) from exc


@router.post("/candidates/{candidate_id}/approve", response_model=TemplateCandidateRead)
def approve_candidate(
    candidate_id: str,
    decision: CandidateDecision,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateCandidateRead:
    try:
        return TemplateCandidateRead.model_validate(
            service.approve_candidate(db, candidate_id, decision)
        )
    except TemplateKnowledgeDomainError as exc:
        raise _http_error(exc) from exc


@router.post("/candidates/{candidate_id}/reject", response_model=TemplateCandidateRead)
def reject_candidate(
    candidate_id: str,
    decision: CandidateRejection,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateCandidateRead:
    try:
        return TemplateCandidateRead.model_validate(
            service.reject_candidate(db, candidate_id, decision)
        )
    except TemplateKnowledgeDomainError as exc:
        raise _http_error(exc) from exc


@router.get("/templates", response_model=list[TemplateVersionRead])
def list_template_versions(
    template_type: TemplateType | None = None,
    version_status: TemplateVersionStatus | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    service: Service = None,
) -> list[TemplateVersionRead]:
    return service.list_versions(db, template_type=template_type, status=version_status)


@router.post(
    "/trade-area/analyze",
    response_model=TemplateKnowledgeRunCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def analyze_trade_area(
    request: TradeAreaAnalyzeRequest,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateKnowledgeRunCreateResponse:
    _require_runtime(TemplateKnowledgeOperation.TRADE_AREA_ANALYSIS)
    return _start_run(
        db,
        service,
        TemplateKnowledgeOperation.TRADE_AREA_ANALYSIS,
        request.model_dump(mode="json"),
    )


@router.get("/runs/{run_id}", response_model=TemplateKnowledgeRunRead)
def get_template_knowledge_run(
    run_id: str,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateKnowledgeRunRead:
    try:
        return TemplateKnowledgeRunRead.model_validate(service.get_run(db, run_id))
    except TemplateKnowledgeDomainError as exc:
        raise _http_error(exc) from exc


@router.get("/runs/{run_id}/result", response_model=TemplateKnowledgeRunResult)
def get_template_knowledge_result(
    run_id: str,
    db: Session = Depends(get_db),
    service: Service = None,
) -> TemplateKnowledgeRunResult:
    try:
        return service.run_result(db, run_id)
    except TemplateKnowledgeDomainError as exc:
        raise _http_error(exc) from exc


@router.post("/bootstrap", status_code=status.HTTP_201_CREATED)
def bootstrap_template_library(
    db: Session = Depends(get_db),
    service: Service = None,
) -> dict:
    try:
        return seed_template_library(db, service=service)
    except TemplateKnowledgeDomainError as exc:
        raise _http_error(exc) from exc


def _http_error(exc: TemplateKnowledgeDomainError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc), "retryable": exc.retryable},
    )


def _start_run(
    db: Session,
    service: TemplateKnowledgeService,
    operation: TemplateKnowledgeOperation,
    request_payload: dict,
) -> TemplateKnowledgeRunCreateResponse:
    run = service.create_run(db, operation, request_payload)
    try:
        task = enqueue_template_knowledge(run.id)
    except Exception as exc:
        service.mark_enqueue_failed(db, run, "The template knowledge task could not be queued.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "TEMPLATE_RUN_ENQUEUE_FAILED",
                "message": "The template knowledge task could not be queued. Retry the request.",
                "run_id": run.id,
            },
        ) from exc
    run.celery_task_id = task.id
    db.commit()
    return TemplateKnowledgeRunCreateResponse(
        run_id=run.id,
        operation=operation,
        status=TemplateKnowledgeRunStatus(run.status),
        task_id=task.id,
    )


def _require_runtime(operation: TemplateKnowledgeOperation) -> None:
    from app.core.config import get_settings

    runtime = get_settings().template_knowledge_runtime
    required = ["candidate_generation"]
    if operation == TemplateKnowledgeOperation.VIDEO_EDITING_CANDIDATE:
        required.append("reference_video_analysis")
    missing = [name for name in required if not runtime[name]]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"missing_template_dependencies": missing},
        )
