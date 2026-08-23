from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .utils import json_dumps


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    run_at TEXT NOT NULL,
    statuses_json TEXT NOT NULL,
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rankings (
    run_id TEXT NOT NULL,
    challenge_id TEXT NOT NULL,
    final_rank INTEGER,
    final_score REAL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, challenge_id)
);
CREATE INDEX IF NOT EXISTS idx_rankings_challenge ON rankings(challenge_id);
CREATE TABLE IF NOT EXISTS features (
    run_id TEXT NOT NULL,
    challenge_id TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, challenge_id)
);
CREATE TABLE IF NOT EXISTS source_metrics (
    run_id TEXT NOT NULL,
    challenge_id TEXT NOT NULL,
    row_json TEXT NOT NULL,
    PRIMARY KEY (run_id, challenge_id)
);
"""


def initialize_database(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.executescript(SCHEMA)
    connection.commit()
    return connection


def load_previous_ranking(path: str | Path) -> pd.DataFrame:
    db_path = Path(path)
    if not db_path.exists():
        return pd.DataFrame(columns=["challenge_id", "previous_rank", "previous_score"])
    connection = initialize_database(db_path)
    try:
        row = connection.execute(
            "SELECT run_id FROM runs ORDER BY run_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return pd.DataFrame(columns=["challenge_id", "previous_rank", "previous_score"])
        previous = pd.read_sql_query(
            """
            SELECT challenge_id, final_rank AS previous_rank, final_score AS previous_score
            FROM rankings WHERE run_id = ?
            """,
            connection,
            params=(row[0],),
        )
        return previous
    finally:
        connection.close()


def save_run(
    path: str | Path,
    *,
    run_id: str,
    run_at: pd.Timestamp,
    statuses: dict[str, dict[str, Any]],
    config: dict[str, Any],
    ranking: pd.DataFrame,
    features: pd.DataFrame,
    source_metrics: pd.DataFrame,
) -> None:
    connection = initialize_database(path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO runs(run_id, run_at, statuses_json, config_json) VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    run_at.isoformat(),
                    json_dumps(statuses),
                    json_dumps(_redact_config(config)),
                ),
            )
            connection.executemany(
                """
                INSERT INTO rankings(run_id, challenge_id, final_rank, final_score, row_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        str(row["challenge_id"]),
                        int(row.get("final_rank", 0)),
                        float(row.get("final_score", 0.0)),
                        json_dumps(row),
                    )
                    for row in ranking.to_dict(orient="records")
                ],
            )
            connection.executemany(
                "INSERT INTO features(run_id, challenge_id, row_json) VALUES (?, ?, ?)",
                [
                    (run_id, str(row["challenge_id"]), json_dumps(row))
                    for row in features.to_dict(orient="records")
                ],
            )
            connection.executemany(
                "INSERT INTO source_metrics(run_id, challenge_id, row_json) VALUES (?, ?, ?)",
                [
                    (run_id, str(row["challenge_id"]), json_dumps(row))
                    for row in source_metrics.to_dict(orient="records")
                ],
            )
    finally:
        connection.close()


def prune_run_history(
    path: str | Path,
    *,
    retention_days: int = 90,
    min_runs_to_keep: int = 10,
    now: datetime | None = None,
) -> dict[str, int | bool]:
    """Bound the legacy SQLite rank-history file without breaking rank deltas.

    The file is still used by the standalone ranker to compute rank_change and
    score_change. The FastAPI service keeps the latest successful runs here, while
    PostgreSQL remains the API source of truth.
    """

    db_path = Path(path)
    if not db_path.exists():
        return {"enabled": True, "deleted_runs": 0, "remaining_runs": 0, "vacuumed": False}

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff = reference - timedelta(days=max(1, retention_days))
    minimum = max(1, min_runs_to_keep)

    connection = initialize_database(db_path)
    deleted_ids: list[str] = []
    try:
        rows = connection.execute(
            "SELECT run_id, run_at FROM runs ORDER BY run_at DESC"
        ).fetchall()
        keep_ids = {str(row[0]) for row in rows[:minimum]}
        for run_id, raw_run_at in rows:
            parsed = _parse_run_at(str(raw_run_at))
            if str(run_id) not in keep_ids and parsed < cutoff:
                deleted_ids.append(str(run_id))

        if deleted_ids:
            placeholders = ",".join("?" for _ in deleted_ids)
            with connection:
                for table in ("rankings", "features", "source_metrics"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE run_id IN ({placeholders})",
                        deleted_ids,
                    )
                connection.execute(
                    f"DELETE FROM runs WHERE run_id IN ({placeholders})",
                    deleted_ids,
                )
            connection.execute("VACUUM")

        remaining = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        return {
            "enabled": True,
            "deleted_runs": len(deleted_ids),
            "remaining_runs": remaining,
            "vacuumed": bool(deleted_ids),
        }
    finally:
        connection.close()


def _parse_run_at(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    # The config only stores environment variable names, not credentials, but redact defensively.
    serialized = json.loads(json.dumps(config, default=str))
    for source in serialized.get("sources", {}).values():
        if not isinstance(source, dict):
            continue
        for key in list(source):
            if "secret" in key.lower() or "token" in key.lower() or key.lower().endswith("key"):
                if not key.lower().endswith("_env"):
                    source[key] = "***"
    return serialized
