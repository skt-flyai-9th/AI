from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.agents.registry import get_agent_definition, list_agent_definitions
from app.core.security import require_internal_api_key

router = APIRouter(
    prefix="/agents",
    tags=["agents"],
    dependencies=[Depends(require_internal_api_key)],
)


@router.get("")
def list_agents() -> dict:
    results = list_agent_definitions()
    return {"count": len(results), "results": results}


@router.get("/{agent_id}")
def get_agent(agent_id: str) -> dict:
    definition = get_agent_definition(agent_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return definition
