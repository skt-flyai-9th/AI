from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.pipeline_run import PipelineRun
from app.models.ranking_snapshot import RankingSnapshot
from app.ranker_core.db import prune_run_history

_FAILURE_STATUSES = ("FAILED", "CANCELLED", "REVOKED")


def cleanup_history(
    db: Session,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Prune terminal run history while preserving recent successful snapshots."""

    resolved = settings or get_settings()
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    if not resolved.history_cleanup_enabled:
        return {
            "enabled": False,
            "postgres": {"deleted_runs": 0, "deleted_snapshots": 0},
            "legacy_sqlite": {"enabled": False, "deleted_runs": 0, "remaining_runs": 0},
        }

    success_cutoff = reference - timedelta(days=resolved.run_retention_days)
    failure_cutoff = reference - timedelta(days=resolved.failed_run_retention_days)

    protected_success_ids = set(
        db.scalars(
            select(PipelineRun.id)
            .where(PipelineRun.status == "COMPLETED")
            .order_by(
                PipelineRun.finished_at.desc().nullslast(),
                PipelineRun.created_at.desc(),
            )
            .limit(resolved.min_successful_runs_to_keep)
        )
    )

    old_successes = list(
        db.scalars(
            select(PipelineRun).where(
                PipelineRun.status == "COMPLETED",
                or_(
                    and_(
                        PipelineRun.finished_at.is_not(None),
                        PipelineRun.finished_at < success_cutoff,
                    ),
                    and_(
                        PipelineRun.finished_at.is_(None),
                        PipelineRun.created_at < success_cutoff,
                    ),
                ),
            )
        )
    )
    old_failures = list(
        db.scalars(
            select(PipelineRun).where(
                PipelineRun.status.in_(_FAILURE_STATUSES),
                or_(
                    and_(
                        PipelineRun.finished_at.is_not(None),
                        PipelineRun.finished_at < failure_cutoff,
                    ),
                    and_(
                        PipelineRun.finished_at.is_(None),
                        PipelineRun.created_at < failure_cutoff,
                    ),
                ),
            )
        )
    )

    targets: dict[str, PipelineRun] = {
        run.id: run for run in old_successes if run.id not in protected_success_ids
    }
    targets.update({run.id: run for run in old_failures})
    target_ids = list(targets)

    deleted_snapshots = 0
    if target_ids:
        deleted_snapshots = int(
            db.scalar(
                select(func.count(RankingSnapshot.id)).where(
                    RankingSnapshot.run_id.in_(target_ids)
                )
            )
            or 0
        )
        for run in targets.values():
            db.delete(run)
        db.commit()

    legacy_path = resolved.ranker_data_dir / "ranker-history.sqlite3"
    legacy_result = prune_run_history(
        legacy_path,
        retention_days=resolved.run_retention_days,
        min_runs_to_keep=resolved.min_successful_runs_to_keep,
        now=reference,
    )

    return {
        "enabled": True,
        "cutoffs": {
            "completed_before": success_cutoff.isoformat(),
            "failed_before": failure_cutoff.isoformat(),
        },
        "postgres": {
            "deleted_runs": len(target_ids),
            "deleted_snapshots": deleted_snapshots,
            "protected_successful_runs": len(protected_success_ids),
        },
        "legacy_sqlite": legacy_result,
    }
