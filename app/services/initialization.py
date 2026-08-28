from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.challenge_ranking.trendcluster import TRENDCLUSTER_FILENAME
from app.core.config import get_settings
from app.models.pipeline_run import PipelineRun
from app.services.pipeline import create_run, execute_pipeline, export_trendcluster
from app.template_knowledge.seeds import seed_template_library


def initialize_service_once(
    db: Session,
    *,
    ranking_executor: Callable[[Session, str], PipelineRun] = execute_pipeline,
) -> dict[str, Any]:
    """Import authoritative data and run research only when no success exists.

    The initializer itself may run after each deployment. Its operations are
    idempotent: source imports only create missing/repaired versions and trend
    research never runs again after the first COMPLETED pipeline run.
    """

    database_result = seed_template_library(db)
    completed = db.scalar(
        select(PipelineRun)
        .where(PipelineRun.status == "COMPLETED")
        .order_by(PipelineRun.finished_at.desc().nullslast(), PipelineRun.created_at.desc())
        .limit(1)
    )
    if completed is None:
        run = create_run(db)
        completed = ranking_executor(db, run.id)
        ranking_result = {
            "status": completed.status,
            "run_id": completed.id,
            "executed": True,
        }
    else:
        ranking_result = {
            "status": "SKIPPED",
            "run_id": completed.id,
            "executed": False,
            "reason": "A completed initial ranking already exists.",
        }
        export_path = get_settings().export_dir / TRENDCLUSTER_FILENAME
        if database_result["created"] or not export_path.is_file():
            export_trendcluster(db)

    return {
        "mode": "INITIAL_ONCE",
        "database_library": database_result,
        "ranking": ranking_result,
        "recurring_content_updates_enabled": False,
    }
