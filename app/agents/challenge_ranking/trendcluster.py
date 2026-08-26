from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

TRENDCLUSTER_FILENAME = "trendcluster.json"

# 운영 trendcluster는 영상편집 DB에서 검증을 마친 이 세 영상만 사용한다.
# 파이프라인이 더 많은 후보를 찾아도 API/DB에 다시 유입시키지 않는다.
TRENDCLUSTER_CHALLENGE_IDS = (
    "jujutsu_transition",
    "cafe_recommendation_reels",
    "otsukare_summer_challenge",
)

_SEED_CATEGORIES = {
    "jujutsu_transition": "meme",
    "cafe_recommendation_reels": "food",
    "otsukare_summer_challenge": "challenge",
}

# These labels describe the three guide videos that were actually analysed in
# the bundled video-editing DB.  Duration and difficulty are derived from the
# analysis rows below; type and whether a face is required are classification
# results that are stable for each analysed guide.
_GUIDE_CLASSIFICATIONS: dict[str, tuple[str, bool]] = {
    "jujutsu_transition": ("밈", False),
    "otsukare_summer_challenge": ("챌린지", True),
    "cafe_recommendation_reels": ("정보형", False),
}


def _difficulty_label(source_lines: list[dict[str, Any]], challenge_id: str) -> str | None:
    """Return 하/중/상 from the shooting-difficulty stars in the analysis."""

    for row in source_lines:
        if row.get("guide_id") != challenge_id:
            continue
        line = str(row.get("raw_markdown_line") or "")
        if "난이도" not in line and "shooting_difficulty" not in line:
            continue
        match = re.search(r"(?:촬영|shooting_difficulty[^★]*)\s*(★{1,3})", line)
        if match:
            return {1: "하", 2: "중", 3: "상"}[len(match.group(1))]
    return None


def _video_format_metadata(source: dict[str, Any], challenge_id: str) -> dict[str, Any]:
    segments = [
        row
        for row in source["datasets"]["03_GUIDE_TEMPLATES"]["records"]
        if row.get("challenge_id") == challenge_id
        and str(row.get("template_status") or "").upper() == "ACTIVE"
    ]
    end_ms = max((float(row.get("end_ms") or 0) for row in segments), default=0)
    format_type, requires_face = _GUIDE_CLASSIFICATIONS[challenge_id]
    source_lines = list(source["datasets"]["12_SOURCE_GUIDES"]["records"])
    return {
        "format_type": format_type,
        # API 5.1 defines this as the completed video's duration, not filming time.
        "expected_duration_sec": math.ceil(end_ms / 1000) if end_ms else None,
        "shooting_difficulty": _difficulty_label(source_lines, challenge_id),
        # Preserve the AI template's native boolean contract. Presentation
        # labels belong to downstream services and clients.
        "requires_face": requires_face,
    }


@lru_cache(maxsize=1)
def _video_format_metadata_by_challenge() -> dict[str, dict[str, Any]]:
    source_path = resources.files("app.template_knowledge.sources").joinpath("video_editing.json")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    return {
        challenge_id: _video_format_metadata(source, challenge_id)
        for challenge_id in _GUIDE_CLASSIFICATIONS
    }


def get_video_format_metadata(challenge_id: str) -> dict[str, Any]:
    """Return grounded card metadata for a bundled, analysed guide."""

    return dict(_video_format_metadata_by_challenge().get(challenge_id, {}))

def build_video_editing_db_trendcluster() -> dict[str, Any]:
    """Build the initial trendcluster from the provided video-editing DB."""

    source_path = resources.files("app.template_knowledge.sources").joinpath("video_editing.json")
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
        challenge_id = str(row["id"])
        if challenge_id not in _SEED_CATEGORIES:
            raise ValueError(f"Missing seed category for trendcluster entry: {challenge_id}")
        guide_url = str(row.get("guide_youtube_url") or "").strip() or None
        generated = str(row.get("generated_at") or "").strip()
        if generated:
            generated_at.append(generated)
        results.append(
            {
                "id": challenge_id,
                "rank": int(rank),
                "name": str(row["name"]),
                "category": _SEED_CATEGORIES[challenge_id],
                "representative_youtube_url": guide_url,
                "guide_youtube_url": guide_url,
                **_video_format_metadata(source, str(row["id"])),
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
