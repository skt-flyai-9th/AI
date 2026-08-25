from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse

from app.api.v1.router import api_router
from app.agents.shortform.seeds import seed_packaged_editing_templates
from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.session import SessionLocal

settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.app_env.lower() in {"local", "test"}:
        init_db()
    with SessionLocal() as db:
        seed_packaged_editing_templates(db)
    yield


app = FastAPI(
    title=settings.app_name,
    version="1.3.0",
    description=(
        "Independent AI server called by the main backend. "
        "Available agents include Korean trend research (challenge-ranking) "
        "the conversational Shortform Agent, and the Editing Agent."
    ),
    default_response_class=ORJSONResponse,
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
def root() -> dict:
    return {
        "service": settings.app_name,
        "role": "independent-ai-server",
        "docs": "/docs",
        "agents": f"{settings.api_v1_prefix}/agents",
        "health": f"{settings.api_v1_prefix}/health/ready",
        "current_challenge_ranking": f"{settings.api_v1_prefix}/challenges?limit=100",
        "shortform_sessions": f"{settings.api_v1_prefix}/shortform-sessions",
        "editing_runs": f"{settings.api_v1_prefix}/editing-runs",
    }
