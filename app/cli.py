from __future__ import annotations

import json
from pathlib import Path

import typer

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.schemas.challenge import OverrideImportItem
from app.services.challenges import import_override_items
from app.services.pipeline import create_run, execute_pipeline, export_latest_json

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


@app.command("import-overrides")
def import_overrides(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = [OverrideImportItem.model_validate(item) for item in payload]
    with SessionLocal() as db:
        updated, missing = import_override_items(db, items)
        export_latest_json(db)
    typer.echo(json.dumps({"updated": updated, "missing": missing}, ensure_ascii=False))


@app.command("export-ranking")
def export_ranking() -> None:
    with SessionLocal() as db:
        path = export_latest_json(db)
    typer.echo(str(path))


if __name__ == "__main__":
    app()
