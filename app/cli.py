from __future__ import annotations

import json
from pathlib import Path

import typer

from app.agents.challenge_ranking.trendcluster import (
    sync_video_editing_db_trendcluster,
)
from app.core.config import get_settings
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.schemas.challenge import OverrideImportItem
from app.services.challenges import import_override_items
from app.services.pipeline import create_run, execute_pipeline, export_trendcluster
from app.services.retention import cleanup_history
from app.schemas.template_knowledge import (
    CandidateDecision,
    EditingCandidateCreate,
    TradeAreaAnalyzeRequest,
    TradeAreaCandidateCreate,
)
from app.template_knowledge.seeds import seed_template_library
from app.template_knowledge.service import TemplateKnowledgeService
from app.template_knowledge.source_library import TemplateSourceService

app = typer.Typer(no_args_is_help=True)


@app.command("init-db")
def init_database() -> None:
    init_db()
    typer.echo("Database initialized.")


@app.command("run-ranking")
def run_ranking() -> None:
    init_db()
    with SessionLocal() as db:
        run = create_run(db)
        typer.echo(f"run_id={run.id}")
        completed = execute_pipeline(db, run.id)
        typer.echo(f"status={completed.status}")


@app.command("cleanup-history")
def cleanup_history_command() -> None:
    init_db()
    with SessionLocal() as db:
        result = cleanup_history(db)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("import-overrides")
def import_overrides(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = [OverrideImportItem.model_validate(item) for item in payload]
    with SessionLocal() as db:
        updated, missing = import_override_items(db, items)
        export_trendcluster(db)
    typer.echo(json.dumps({"updated": updated, "missing": missing}, ensure_ascii=False))


@app.command("export-trendcluster")
def export_trendcluster_command() -> None:
    with SessionLocal() as db:
        path = export_trendcluster(db)
    typer.echo(str(path))


@app.command("sync-trendcluster-from-video-editing-db")
def sync_trendcluster_from_video_editing_db() -> None:
    """Replace trendcluster with the three entries in the provided video-editing DB."""

    path = sync_video_editing_db_trendcluster(get_settings().export_dir)
    typer.echo(str(path))


@app.command("import-database-library")
def import_databases() -> None:
    """Import the user-provided video-editing and trade-area databases."""

    _import_databases()


def _import_databases() -> None:
    init_db()
    with SessionLocal() as db:
        result = seed_template_library(db)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=str))


@app.command("resolve-trade-area-db-context")
def resolve_trade_area_db_context(
    region_id: str | None = None,
    category_id: str | None = None,
    official_trade_area_code: str | None = None,
    include_draft: bool = False,
) -> None:
    init_db()
    with SessionLocal() as db:
        result = TemplateSourceService().resolve_trade_area_context(
            db,
            region_id=region_id,
            category_id=category_id,
            official_trade_area_code=official_trade_area_code,
            include_draft=include_draft,
        )
    typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


@app.command("generate-trade-area-db")
def generate_trade_area_db(
    database_id: str,
    evidence_path: Path,
) -> None:
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    manager = TemplateKnowledgeService()
    with SessionLocal() as db:
        candidate = manager.create_trade_area_candidate(
            db,
            TradeAreaCandidateCreate(template_id=database_id, evidence=payload),
        )
    typer.echo(
        json.dumps(
            {"candidate_id": candidate.id, "status": candidate.status},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("generate-video-editing-db")
def generate_video_editing_db(
    database_id: str,
    trend_id: list[str] | None = typer.Option(None, "--trend-id"),
) -> None:
    manager = TemplateKnowledgeService()
    with SessionLocal() as db:
        candidate = manager.create_editing_candidate(
            db,
            EditingCandidateCreate(template_id=database_id, trend_ids=trend_id or []),
        )
    typer.echo(
        json.dumps(
            {"candidate_id": candidate.id, "status": candidate.status},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("approve-database-candidate")
def approve_database_candidate(
    candidate_id: str,
    actor: str,
    note: str = "",
) -> None:
    manager = TemplateKnowledgeService()
    with SessionLocal() as db:
        candidate = manager.approve_candidate(
            db, candidate_id, CandidateDecision(actor=actor, note=note)
        )
    typer.echo(
        json.dumps(
            {"candidate_id": candidate.id, "status": candidate.status},
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("analyze-trade-area-db")
def analyze_trade_area_db(evidence_path: Path, database_id: str | None = None) -> None:
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    manager = TemplateKnowledgeService()
    with SessionLocal() as db:
        result = manager.analyze_trade_area(
            db,
            TradeAreaAnalyzeRequest(evidence=payload, template_id=database_id),
        )
    typer.echo(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    app()
