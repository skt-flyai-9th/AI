from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import require_admin_token
from app.db.session import get_db
from app.schemas.challenge import OverrideImportItem
from app.services.challenges import import_override_items
from app.services.pipeline import export_latest_json

router = APIRouter(prefix="/overrides", tags=["overrides"])


@router.post("/import", dependencies=[Depends(require_admin_token)])
def import_overrides(items: list[OverrideImportItem], db: Session = Depends(get_db)) -> dict:
    updated, missing = import_override_items(db, items)
    export_latest_json(db)
    return {"updated": updated, "missing_challenge_ids": missing}
