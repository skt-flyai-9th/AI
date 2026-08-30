from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.config import Settings, get_settings


REMOVED_SHORTFORM_IDS = {
    "cafe_recommendation_reels",
    "donggeurio_store_promotion",
}


def purge_removed_shortform_runtime_data(
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Remove deleted shortforms from the ranker cache and CSV inputs."""

    resolved = settings or get_settings()
    data_dir = resolved.ranker_data_dir.resolve()
    removed_rows: dict[str, int] = {}

    sqlite_path = data_dir / "ranker-history.sqlite3"
    removed_rows[sqlite_path.name] = _purge_sqlite_history(sqlite_path)
    for filename in ("candidates.auto.csv", "observations.csv"):
        csv_path = data_dir / filename
        removed_rows[filename] = _purge_csv(csv_path)

    return {
        "removed_ids": sorted(REMOVED_SHORTFORM_IDS),
        "removed_rows": removed_rows,
    }


def _purge_sqlite_history(path: Path) -> int:
    if not path.is_file():
        return 0
    removed = 0
    placeholders = ",".join("?" for _ in REMOVED_SHORTFORM_IDS)
    with sqlite3.connect(path) as connection:
        existing_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in ("rankings", "features", "source_metrics"):
            if table not in existing_tables:
                continue
            cursor = connection.execute(
                f"DELETE FROM {table} WHERE challenge_id IN ({placeholders})",  # noqa: S608
                tuple(sorted(REMOVED_SHORTFORM_IDS)),
            )
            removed += max(cursor.rowcount, 0)
        connection.commit()
    return removed


def _purge_csv(path: Path) -> int:
    if not path.is_file():
        return 0
    frame = pd.read_csv(path)
    if "challenge_id" not in frame.columns:
        return 0
    before = len(frame)
    filtered = frame[
        ~frame["challenge_id"].fillna("").astype(str).isin(REMOVED_SHORTFORM_IDS)
    ].copy()
    if len(filtered) == before:
        return 0
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    filtered.to_csv(temporary, index=False, encoding="utf-8-sig")
    temporary.replace(path)
    return before - len(filtered)
