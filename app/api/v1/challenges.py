from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import require_admin_token
from app.db.session import get_db
from app.models.challenge import Challenge
from app.schemas.challenge import ChallengeListResponse, ChallengeRead, ChallengeUpdate
from app.services.challenges import get_latest_generated_at, list_challenges, to_read, apply_update
from app.services.pipeline import export_latest_json

router = APIRouter(prefix="/challenges", tags=["challenges"])


@router.get("", response_model=ChallengeListResponse)
def get_challenges(
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    include_inactive: bool = False,
    db: Session = Depends(get_db),
) -> ChallengeListResponse:
    rows = list_challenges(db, limit=limit, offset=offset, include_inactive=include_inactive)
    return ChallengeListResponse(
        generated_at=get_latest_generated_at(db),
        count=len(rows),
        results=[to_read(row) for row in rows],
    )


@router.get("/{challenge_id}", response_model=ChallengeRead)
def get_challenge(challenge_id: str, db: Session = Depends(get_db)) -> ChallengeRead:
    row = db.get(Challenge, challenge_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Challenge not found")
    return to_read(row)


@router.patch(
    "/{challenge_id}",
    response_model=ChallengeRead,
    dependencies=[Depends(require_admin_token)],
)
def update_challenge(
    challenge_id: str,
    payload: ChallengeUpdate,
    db: Session = Depends(get_db),
) -> ChallengeRead:
    row = db.get(Challenge, challenge_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Challenge not found")
    apply_update(row, payload)
    db.commit()
    db.refresh(row)
    export_latest_json(db)
    return to_read(row)
