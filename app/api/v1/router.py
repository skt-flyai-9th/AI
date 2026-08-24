from fastapi import APIRouter

from app.api.v1 import (
    agents,
    challenges,
    editing_templates,
    health,
    overrides,
    ranking_runs,
    shortform_sessions,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(agents.router)
api_router.include_router(challenges.router)
api_router.include_router(ranking_runs.router)
api_router.include_router(overrides.router)
api_router.include_router(shortform_sessions.router)
api_router.include_router(editing_templates.router)
