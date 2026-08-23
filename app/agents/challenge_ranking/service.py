from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.pipeline_run import PipelineRun
from app.models.ranking_snapshot import RankingSnapshot
from app.services.pipeline import create_run as create_run
from app.services.pipeline import execute_pipeline as execute_pipeline
from app.services.pipeline import export_latest_json as export_latest_json
from app.services.pipeline import validate_runtime_keys as validate_runtime_keys


def get_run_result_payload(
    db: Session,
    run_id: str,
    *,
    limit: int = 100,
) -> tuple[PipelineRun | None, list[dict[str, Any]]]:
    """Return the immutable automatic result stored for one pipeline run."""

    run = db.get(PipelineRun, run_id)
    if run is None:
        return None, []

    snapshots = list(
        db.scalars(
            select(RankingSnapshot)
            .where(RankingSnapshot.run_id == run_id)
            .order_by(RankingSnapshot.automatic_rank.asc())
            .limit(limit)
        )
    )

    results: list[dict[str, Any]] = []
    for snapshot in snapshots:
        row = snapshot.row_data or {}
        results.append(
            {
                "id": snapshot.challenge_id,
                "rank": snapshot.automatic_rank,
                "name": str(row.get("name") or snapshot.challenge_id),
                "representative_youtube_url": (
                    str(row.get("representative_youtube_url"))
                    if row.get("representative_youtube_url")
                    else None
                ),
                "guide_youtube_url": (
                    str(row.get("guide_youtube_url"))
                    if row.get("guide_youtube_url")
                    else None
                ),
            }
        )

    return run, results
