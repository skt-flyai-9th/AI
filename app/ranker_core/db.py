from __future__ import annotations

import json
import sqlite3
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
