from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.challenge_ranking.harness import challenge_ranking_harness
from app.agents.challenge_ranking.trendcluster import write_trendcluster
from app.core.config import get_settings
from app.models.challenge import Challenge
from app.models.pipeline_run import PipelineRun
from app.models.ranking_snapshot import RankingSnapshot
from app.ranker_core.pipeline import run_pipeline
from app.ranker_core.representative import extract_youtube_video_id, youtube_watch_url


class TrendResearchIncomplete(RuntimeError):
    """The research run did not produce the requested full URL-backed ranking."""


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _row_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k): _json_value(v) for k, v in row.items()}


def build_runtime_config() -> dict[str, Any]:
    settings = get_settings()
    config_path = settings.pipeline_config_path.resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    data_dir = settings.ranker_data_dir.resolve()
    export_dir = settings.export_dir.resolve()
    research_output_dir = data_dir / "research-outputs"
    data_dir.mkdir(parents=True, exist_ok=True)
    export_dir.mkdir(parents=True, exist_ok=True)
    research_output_dir.mkdir(parents=True, exist_ok=True)
    config.setdefault("paths", {})
    config["paths"].update(
        {
            "candidates_csv": str(data_dir / "candidates.auto.csv"),
            "observations_csv": str(data_dir / "observations.csv"),
            "database": str(data_dir / "ranker-history.sqlite3"),
            # Ranker diagnostics are staged away from the public trendcluster.
            # export_trendcluster() replaces the public file only after the
            # complete Top 100 commits successfully.
            "output_dir": str(research_output_dir),
        }
    )
    ranking = config.setdefault("ranking", {})
    ranking["top_n"] = 100
    ranking["require_youtube_video"] = True
    ranking.pop("exclude_challenge_ids", None)
    return config


def validate_runtime_keys() -> dict[str, bool]:
    return get_settings().required_api_key_status


def create_run(db: Session) -> PipelineRun:
    run = PipelineRun(id=str(uuid.uuid4()), status="QUEUED", stage="QUEUED", progress=0)
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def execute_pipeline(db: Session, run_id: str) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise ValueError(f"PipelineRun not found: {run_id}")

    run.status = "RUNNING"
    run.stage = "COLLECTING_AND_ANALYZING"
    run.progress = 10
    run.started_at = datetime.now(timezone.utc)
    db.commit()

    try:
        config = build_runtime_config()
        result = challenge_ranking_harness.execute(
            operation="research",
            input_value=config,
            executor=run_pipeline,
            correlation_id=run.id,
        )
        run.stage = "PERSISTING"
        run.progress = 85
        run.source_status = result.statuses
        run.warnings = _status_warnings(result.statuses)
        db.commit()

        persist_result(
            db,
            run,
            result.ranking,
            result.source_metrics,
            expected_count=int(config["ranking"]["top_n"]),
        )
        export_trendcluster(db)

        run.status = "COMPLETED"
        run.stage = "COMPLETED"
        run.progress = 100
        run.finished_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        run = db.get(PipelineRun, run_id)
        if run is not None:
            run.status = "FAILED"
            run.stage = "FAILED"
            run.error_message = str(exc)[:4000]
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        raise


def _status_warnings(statuses: dict[str, dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for source, status in statuses.items():
        if status.get("enabled") is False or status.get("skipped"):
            continue
        if not status.get("success", False):
            reason = status.get("error") or status.get("reason") or "unknown error"
            warnings.append(f"{source}: {reason}")
    return warnings


def persist_result(
    db: Session,
    run: PipelineRun,
    ranking: pd.DataFrame,
    source_metrics: pd.DataFrame,
    *,
    expected_count: int = 100,
) -> None:
    now = datetime.now(timezone.utc)
    metrics_by_id: dict[str, dict[str, Any]] = {}
    if source_metrics is not None and not source_metrics.empty:
        for row in source_metrics.to_dict(orient="records"):
            metrics_by_id[str(row.get("challenge_id"))] = _row_dict(row)

    researched = _select_researched_trends(ranking, limit=expected_count)
    if len(researched) != expected_count:
        raise TrendResearchIncomplete(
            f"Expected {expected_count} unique YouTube-backed trends, "
            f"found {len(researched)}. The existing ranking was not changed."
        )

    # Replace the active ranking only after the complete validated batch exists.
    # Historical rows and snapshots remain queryable, but stale challenges are
    # no longer exposed by the current ranking APIs or export.
    selected_ids = {str(row["challenge_id"]) for row in researched}
    for challenge in db.scalars(select(Challenge)):
        if challenge.id not in selected_ids:
            challenge.active = False

    for offset, row in enumerate(researched):
        challenge_id = str(row["challenge_id"])
        assigned_rank = offset + 1
        challenge = db.get(Challenge, challenge_id)
        if challenge is None:
            challenge = Challenge(
                id=challenge_id,
                automatic_name=str(row.get("name") or challenge_id),
                first_seen_at=now,
            )
            db.add(challenge)

        challenge.automatic_name = str(row.get("name") or challenge_id)
        challenge.aliases = list(row.get("alias_list") or [])
        challenge.category = str(row.get("category") or "unknown")
        challenge.automatic_rank = assigned_rank
        challenge.automatic_score = float(row.get("final_score") or 0.0)
        challenge.lifecycle = str(row.get("stage") or "UNKNOWN")
        challenge.kr_affinity = float(row.get("kr_affinity") or 0.0)
        challenge.confidence = float(row.get("confidence") or 0.0)
        challenge.automatic_representative_youtube_url = (
            str(row.get("representative_youtube_url") or "") or None
        )
        challenge.automatic_guide_youtube_url = str(row.get("guide_youtube_url") or "") or None
        challenge.representative_video_metadata = {
            "video_id": row.get("representative_youtube_video_id"),
            "title": row.get("representative_youtube_title"),
            "channel": row.get("representative_youtube_channel"),
            "views": row.get("representative_youtube_views"),
            "score": row.get("representative_youtube_score"),
            "participation_type": row.get("representative_youtube_participation_type"),
        }
        challenge.guide_video_metadata = {
            "video_id": row.get("guide_youtube_video_id"),
            "title": row.get("guide_youtube_title"),
            "channel": row.get("guide_youtube_channel"),
            "views": row.get("guide_youtube_views"),
            "score": row.get("guide_youtube_score"),
            "guide_type": row.get("guide_youtube_type"),
        }
        challenge.raw_details = row
        challenge.raw_details["research_rank"] = assigned_rank
        challenge.raw_details["research_auto_activated"] = True
        challenge.latest_run_id = run.id
        challenge.active = True
        challenge.last_seen_at = now

        db.flush()
        db.add(
            RankingSnapshot(
                run_id=run.id,
                challenge_id=challenge_id,
                automatic_rank=assigned_rank,
                automatic_score=float(row.get("final_score") or 0.0),
                row_data=row,
                source_metrics=metrics_by_id.get(challenge_id, {}),
            )
        )
    db.commit()


def _select_researched_trends(
    ranking: pd.DataFrame,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Pick the best unique social trends with usable YouTube URLs."""

    if ranking is None or ranking.empty:
        return []
    ranked = ranking.copy()
    if "final_rank" not in ranked.columns:
        return []
    if "confidence" not in ranked.columns:
        ranked["confidence"] = 0.0
    ranked = ranked.sort_values(["final_rank", "confidence"], ascending=[True, False])

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in ranked.to_dict(orient="records"):
        row = _row_dict(raw)
        challenge_id = str(row.get("challenge_id") or "").strip()
        if not challenge_id or challenge_id in seen:
            continue
        if "is_social_challenge" in row and not bool(row.get("is_social_challenge")):
            continue

        representative_id = extract_youtube_video_id(row.get("representative_youtube_url"))
        guide_id = extract_youtube_video_id(row.get("guide_youtube_url"))
        usable_id = representative_id or guide_id
        if not usable_id:
            continue
        if not representative_id:
            row["representative_youtube_url"] = youtube_watch_url(usable_id)
            row["representative_youtube_video_id"] = usable_id
        if not guide_id:
            row["guide_youtube_url"] = youtube_watch_url(usable_id)
            row["guide_youtube_video_id"] = usable_id

        seen.add(challenge_id)
        selected.append(row)
        if len(selected) == limit:
            break
    return selected


def export_trendcluster(db: Session) -> Path:
    from app.services.challenges import (
        active_template_refs,
        effective_rank_expression,
        to_export_record,
    )
    from sqlalchemy import select

    settings = get_settings()
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    rows = list(
        db.scalars(
            select(Challenge)
            .where(Challenge.active.is_(True))
            .order_by(
                effective_rank_expression().asc().nullslast(), Challenge.automatic_score.desc()
            )
            .limit(100)
        )
    )
    template_refs = active_template_refs(db, {row.id for row in rows})
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "results": [to_export_record(row, template_refs.get(row.id)) for row in rows],
    }
    return write_trendcluster(payload, settings.export_dir)
