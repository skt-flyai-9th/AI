from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.challenge_ranking.trendcluster import (
    TRENDCLUSTER_CHALLENGE_IDS,
    TRENDCLUSTER_FILENAME,
    build_video_editing_db_trendcluster,
)
from app.core.config import get_settings
from app.models.challenge import Challenge
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
    bundled_challenges = _sync_bundled_challenges(db)
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
        "bundled_challenges": bundled_challenges,
        "ranking": ranking_result,
        "recurring_content_updates_enabled": False,
    }


def _sync_bundled_challenges(db: Session) -> dict[str, list[str]]:
    """Upsert the authoritative two-item trendcluster without rerunning research."""

    created: list[str] = []
    updated: list[str] = []
    now = datetime.now(timezone.utc)
    payload = build_video_editing_db_trendcluster()
    for item in payload["results"]:
        challenge_id = str(item["id"])
        challenge = db.get(Challenge, challenge_id)
        if challenge is None:
            challenge = Challenge(
                id=challenge_id,
                automatic_name=str(item["name"]),
                first_seen_at=now,
            )
            db.add(challenge)
            created.append(challenge_id)
        else:
            updated.append(challenge_id)
        challenge.automatic_name = str(item["name"])
        challenge.category = str(item["category"])
        challenge.automatic_rank = int(item["rank"])
        challenge.automatic_representative_youtube_url = item.get("representative_youtube_url")
        challenge.automatic_guide_youtube_url = item.get("guide_youtube_url")
        challenge.lifecycle = "ACTIVE"
        challenge.confidence = 1.0
        challenge.active = True
        challenge.raw_details = dict(item)
        challenge.last_seen_at = now

    for challenge in db.scalars(select(Challenge)):
        if challenge.id not in TRENDCLUSTER_CHALLENGE_IDS:
            challenge.active = False
    db.commit()
    return {"created": created, "updated": updated}
