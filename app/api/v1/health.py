from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.agents.editing.service import validate_editing_runtime
from app.agents.registry import list_agent_definitions
from app.core.config import get_settings
from app.db.session import get_db
from app.models.editing_template import EditingTemplate

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    settings = get_settings()
    editing_runtime = validate_editing_runtime()
    active_template_count = db.scalar(
        select(func.count()).select_from(EditingTemplate).where(EditingTemplate.status == "ACTIVE")
    ) or 0
    payload = {
        "status": "ready",
        "agents": [item["id"] for item in list_agent_definitions()],
        "api_keys": settings.required_api_key_status,
        "shortform_llm_ready": settings.shortform_llm_ready,
        "editing_runtime_ready": all(editing_runtime.values()),
        "editing_runtime": editing_runtime,
        "internal_auth_configured": bool(settings.effective_internal_api_key),
        "active_editing_template_count": active_template_count,
    }
    if active_template_count < 1:
        payload["status"] = "not_ready"
        raise HTTPException(status_code=503, detail=payload)
    return payload
