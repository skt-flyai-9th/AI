from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents.editing.service import validate_editing_runtime
from app.agents.registry import list_agent_definitions
from app.core.config import get_settings
from app.core.security import require_internal_api_key
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ready"}


@router.get("/health/diagnostics", dependencies=[Depends(require_internal_api_key)])
def diagnostics(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    settings = get_settings()
    database_runtime = settings.database_knowledge_runtime
    editing_runtime = validate_editing_runtime()
    return {
        "status": "ready",
        "agents": [item["id"] for item in list_agent_definitions()],
        "api_keys": settings.required_api_key_status,
        "shortform_llm_ready": settings.shortform_llm_ready,
        "editing_runtime_ready": all(editing_runtime.values()),
        "editing_runtime": editing_runtime,
        "database_knowledge_ready": all(database_runtime.values()),
        "database_knowledge_runtime": database_runtime,
        "database_human_approval_required": settings.database_require_human_approval,
        "database_maintenance_enabled": settings.database_maintenance_enabled,
        "internal_auth_configured": bool(settings.effective_internal_api_key),
    }
