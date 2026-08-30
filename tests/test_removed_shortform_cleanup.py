from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pandas as pd

from app.services.removed_shortform_cleanup import purge_removed_shortform_runtime_data


def test_runtime_cleanup_removes_deleted_ids_without_touching_retained_rows(tmp_path):
    sqlite_path = tmp_path / "ranker-history.sqlite3"
    with sqlite3.connect(sqlite_path) as connection:
        for table in ("rankings", "features", "source_metrics"):
            connection.execute(
                f"CREATE TABLE {table} (run_id TEXT, challenge_id TEXT, row_json TEXT)"
            )
            connection.executemany(
                f"INSERT INTO {table} VALUES (?, ?, ?)",
                [
                    ("run-1", "jujutsu_transition", "{}"),
                    ("run-1", "cafe_recommendation_reels", "{}"),
                    ("run-1", "donggeurio_challenge", "{}"),
                    ("run-1", "donggeurio_store_promotion", "{}"),
                ],
            )

    for filename in ("candidates.auto.csv", "observations.csv"):
        pd.DataFrame(
            [
                {"challenge_id": "jujutsu_transition", "value": 1},
                {"challenge_id": "cafe_recommendation_reels", "value": 2},
                {"challenge_id": "donggeurio_challenge", "value": 3},
                {"challenge_id": "donggeurio_store_promotion", "value": 4},
            ]
        ).to_csv(tmp_path / filename, index=False)

    result = purge_removed_shortform_runtime_data(SimpleNamespace(ranker_data_dir=tmp_path))

    assert result["removed_rows"] == {
        "ranker-history.sqlite3": 9,
        "candidates.auto.csv": 3,
        "observations.csv": 3,
    }
    with sqlite3.connect(sqlite_path) as connection:
        for table in ("rankings", "features", "source_metrics"):
            assert connection.execute(f"SELECT challenge_id FROM {table}").fetchall() == [
                ("jujutsu_transition",)
            ]
    for filename in ("candidates.auto.csv", "observations.csv"):
        assert pd.read_csv(tmp_path / filename)["challenge_id"].tolist() == ["jujutsu_transition"]
