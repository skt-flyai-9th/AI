from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import require_internal_api_key
from app.db.session import get_db
from app.schemas.challenge import OverrideImportItem
from app.services.challenges import import_override_items
from app.services.pipeline import export_trendcluster

router = APIRouter(
    prefix="/overrides",
    tags=["overrides"],
    dependencies=[Depends(require_internal_api_key)],
)


@router.post("/import")
def import_overrides(items: list[OverrideImportItem], db: Session = Depends(get_db)) -> dict:
    updated, missing = import_override_items(db, items)
    export_trendcluster(db)
    return {"updated": updated, "missing_challenge_ids": missing}
