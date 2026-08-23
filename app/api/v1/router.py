from fastapi import APIRouter

from app.api.v1 import agents, challenges, health, overrides, ranking_runs

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(agents.router)
api_router.include_router(challenges.router)
api_router.include_router(ranking_runs.router)
api_router.include_router(overrides.router)
