from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.agents.shortform.service import (
    ShortformAgentService,
    ShortformDomainError,
    get_shortform_agent_service,
)
from app.core.security import require_internal_api_key
from app.db.session import get_db
from app.schemas.shortform import ShootingGuideResponse

router = APIRouter(
    prefix="/video-editing-db",
    tags=["video-editing-db"],
    dependencies=[Depends(require_internal_api_key)],
)


@router.get(
    "/{record_id}/versions/{version}/shooting-guide",
    response_model=ShootingGuideResponse,
)
def get_shooting_guide(
    record_id: str,
    version: int,
    db: Session = Depends(get_db),
    service: ShortformAgentService = Depends(get_shortform_agent_service),
) -> ShootingGuideResponse:
    try:
        return service.get_shooting_guide(db, record_id, version)
    except ShortformDomainError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        ) from exc
