from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

TRENDCLUSTER_FILENAME = "trendcluster.json"


def build_video_editing_db_trendcluster() -> dict[str, Any]:
    """Build the initial trendcluster from the provided video-editing DB."""

    source_path = resources.files("app.template_knowledge.sources").joinpath(
        "video_editing.json"
    )
    source = json.loads(source_path.read_text(encoding="utf-8"))
    rows = list(source["datasets"]["02_INPUT_GUIDES"]["records"])
    results: list[dict[str, Any]] = []
    generated_at: list[str] = []
    for row in rows:
        if str(row.get("input_validation_status") or "").upper() != "PASS":
            continue
        rank = row.get("rank")
        if rank is None:
            continue
        guide_url = str(row.get("guide_youtube_url") or "").strip() or None
        generated = str(row.get("generated_at") or "").strip()
        if generated:
            generated_at.append(generated)
        results.append(
            {
                "id": str(row["id"]),
                "rank": int(rank),
                "name": str(row["name"]),
                "representative_youtube_url": guide_url,
                "guide_youtube_url": guide_url,
            }
        )
    results.sort(key=lambda item: (item["rank"], item["id"]))
    if len(results) != 3:
        raise ValueError(
            f"Expected exactly 3 PASS entries in the provided video-editing DB, got {len(results)}."
        )
    return {
        "generated_at": max(generated_at),
        "count": len(results),
        "results": results,
    }


def write_trendcluster(payload: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / TRENDCLUSTER_FILENAME
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)
    return path


def sync_video_editing_db_trendcluster(output_dir: Path) -> Path:
    return write_trendcluster(build_video_editing_db_trendcluster(), output_dir)
