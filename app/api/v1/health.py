from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents.editing.service import validate_editing_runtime
from app.agents.registry import list_agent_definitions
from app.core.config import get_settings
from app.db.session import get_db

router = APIRouter(tags=["health"])


@router.get("/health/live")
def live() -> dict:
    return {"status": "ok"}


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    settings = get_settings()
    template_runtime = settings.template_knowledge_runtime
    editing_runtime = validate_editing_runtime()
    return {
        "status": "ready",
        "agents": [item["id"] for item in list_agent_definitions()],
        "api_keys": settings.required_api_key_status,
        "shortform_llm_ready": settings.shortform_llm_ready,
        "editing_runtime_ready": all(editing_runtime.values()),
        "editing_runtime": editing_runtime,
        "template_knowledge_ready": all(template_runtime.values()),
        "template_knowledge_runtime": template_runtime,
        "template_human_approval_required": settings.template_require_human_approval,
        "template_maintenance_enabled": settings.template_maintenance_enabled,
        "internal_auth_configured": bool(settings.effective_internal_api_key),
    }
