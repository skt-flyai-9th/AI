from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
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
    prefix="/editing-templates",
    tags=["editing-templates"],
    dependencies=[Depends(require_internal_api_key)],
)


@router.get(
    "/{template_id}/versions/{version}/shooting-guide",
    response_model=ShootingGuideResponse,
)
def get_shooting_guide(
    template_id: str,
    version: int,
    store_name: str | None = Query(default=None, max_length=120),
    business_type: str | None = Query(default=None, max_length=120),
    promotion_subject: str | None = Query(default=None, max_length=120),
    promotion_objective: str | None = Query(default=None, max_length=120),
    menu_name: str | None = Query(default=None, max_length=120),
    face_exposure: str | None = Query(default=None, max_length=40),
    db: Session = Depends(get_db),
    service: ShortformAgentService = Depends(get_shortform_agent_service),
) -> ShootingGuideResponse:
    try:
        return service.get_shooting_guide(
            db,
            template_id,
            version,
            context={
                "store_name": store_name,
                "business_type": business_type,
                "promotion_subject": promotion_subject,
                "promotion_objective": promotion_objective,
                "menu_name": menu_name,
                "face_exposure": face_exposure,
            },
        )
    except ShortformDomainError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": str(exc),
                "retryable": exc.retryable,
            },
        ) from exc
