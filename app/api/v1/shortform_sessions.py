from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.agents.shortform.llm import ShortformLLMError
from app.agents.shortform.service import (
    ShortformAgentService,
    ShortformDomainError,
    get_shortform_agent_service,
)
from app.core.security import require_internal_api_key
from app.db.session import get_db
from app.schemas.shortform import (
    NextRecommendationResponse,
    ShortformSessionCreateRequest,
    ShortformSessionCreateResponse,
    ShortformTurnRequest,
    ShortformTurnResponse,
)

router = APIRouter(
    prefix="/shortform-sessions",
    tags=["shortform"],
    dependencies=[Depends(require_internal_api_key)],
)


@router.post("", response_model=ShortformSessionCreateResponse, status_code=status.HTTP_200_OK)
def create_shortform_session(
    body: ShortformSessionCreateRequest,
    db: Session = Depends(get_db),
    service: ShortformAgentService = Depends(get_shortform_agent_service),
) -> ShortformSessionCreateResponse:
    return service.create_session(db, body.store_context)


@router.post("/{session_id}/turns", response_model=ShortformTurnResponse)
def create_shortform_turn(
    session_id: str,
    body: ShortformTurnRequest,
    db: Session = Depends(get_db),
    service: ShortformAgentService = Depends(get_shortform_agent_service),
) -> ShortformTurnResponse:
    try:
        return service.process_turn(db, session_id, body.input)
    except ShortformDomainError as exc:
        _raise_domain_error(exc)
    except ShortformLLMError as exc:
        _raise_llm_error(exc)
    raise AssertionError("unreachable")


@router.post(
    "/{session_id}/recommendations/next",
    response_model=NextRecommendationResponse,
)
def next_shortform_recommendation(
    session_id: str,
    db: Session = Depends(get_db),
    service: ShortformAgentService = Depends(get_shortform_agent_service),
) -> NextRecommendationResponse:
    try:
        return service.next_recommendation(db, session_id)
    except ShortformDomainError as exc:
        _raise_domain_error(exc)
    except ShortformLLMError as exc:
        _raise_llm_error(exc)
    raise AssertionError("unreachable")


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shortform_session(
    session_id: str,
    db: Session = Depends(get_db),
    service: ShortformAgentService = Depends(get_shortform_agent_service),
) -> Response:
    try:
        service.delete_session(db, session_id)
    except ShortformDomainError as exc:
        _raise_domain_error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _raise_domain_error(exc: ShortformDomainError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail={
            "code": exc.code,
            "message": str(exc),
            "retryable": exc.retryable,
        },
    ) from exc


def _raise_llm_error(exc: ShortformLLMError) -> None:
    http_status = exc.status_code or status.HTTP_503_SERVICE_UNAVAILABLE
    if http_status == status.HTTP_429_TOO_MANY_REQUESTS:
        code = "SHORTFORM_AGENT_RATE_LIMITED"
        headers = {"Retry-After": "5"}
    elif http_status >= 500:
        code = "SHORTFORM_AGENT_UNAVAILABLE"
        headers = None
    else:
        code = "SHORTFORM_AGENT_LLM_ERROR"
        headers = None
    raise HTTPException(
        status_code=http_status,
        detail={
            "code": code,
            "message": str(exc),
            "retryable": exc.retryable,
        },
        headers=headers,
    ) from exc
