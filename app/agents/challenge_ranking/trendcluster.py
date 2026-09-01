from __future__ import annotations

import copy
import json
import math
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

TRENDCLUSTER_FILENAME = "trendcluster.json"

# The first four rows come from the reviewed video-editing DB and are immutable
# during live research. A successful research run appends eleven automatically
# activated rows at ranks 5..15; there is no human-approval gate for trends.
PINNED_TREND_IDS = (
    "jujutsu_transition",
    "donggeurio_challenge",
    "otsukare_summer_challenge",
    "doma_bad_challenge",
)

PINNED_TREND_RANKS = {
    "jujutsu_transition": 1,
    "donggeurio_challenge": 2,
    "otsukare_summer_challenge": 3,
    "doma_bad_challenge": 4,
}

RESEARCH_TREND_COUNT = 11
RESEARCH_TREND_FIRST_RANK = 5
RESEARCH_TREND_LAST_RANK = RESEARCH_TREND_FIRST_RANK + RESEARCH_TREND_COUNT - 1

# Backward-compatible aliases for seed/import code that still describes the
# bundled four-item dataset as the initial trendcluster.
TRENDCLUSTER_CHALLENGE_IDS = PINNED_TREND_IDS
TRENDCLUSTER_CANONICAL_RANKS = PINNED_TREND_RANKS

_SEED_CATEGORIES = {
    "jujutsu_transition": "meme",
    "donggeurio_challenge": "food",
    "otsukare_summer_challenge": "challenge",
    "doma_bad_challenge": "challenge",
}

_SEED_GUIDE_URL_OVERRIDES = {
    "jujutsu_transition": "https://www.youtube.com/shorts/Aa-CGr9-c8E",
}

# These labels describe the guide videos that were actually analysed in
# the bundled video-editing DB.  Duration and difficulty are derived from the
# analysis rows below; type and whether a face is required are classification
# results that are stable for each analysed guide.
_GUIDE_CLASSIFICATIONS: dict[str, tuple[str, bool]] = {
    "jujutsu_transition": ("밈", False),
    "donggeurio_challenge": ("정보형", False),
    "otsukare_summer_challenge": ("챌린지", True),
    "doma_bad_challenge": ("챌린지", True),
}

_REFERENCE_CUT_REVIEWS: dict[str, dict[str, Any]] = {
    "jujutsu_transition": {
        "status": "HUMAN_REVIEWED",
        "expected_cut_count": 8,
        "boundary_basis": [
            "음식이나 음료가 이전 프레임에 없다가 갑자기 등장하면 별도 컷으로 분리",
            "손동작, 화면 가림, 의상·구도 변경 전후의 프레임 불연속을 각각 컷으로 분리",
            "의미가 같은 변신 장면이어도 물체·인물·구도가 연속되지 않으면 합치지 않음",
        ],
    },
    "donggeurio_challenge": {
        "status": "UPLOADED_JSON_REVIEWED",
        "expected_cut_count": 7,
        "boundary_basis": [
            "업로드 MP4 실측 경계 0·1033·3767·4700·7000·8333·9633·12067ms를 사용",
            "훅·메뉴 공개·공간 팬·메뉴 액션·액션 하이라이트·외관·아웃트로를 각각 분리",
            "의미 앵커를 보존한 뒤 인접 강한 오디오 onset에만 컷을 스냅",
        ],
    },
    "otsukare_summer_challenge": {
        "status": "HUMAN_REVIEWED",
        "expected_cut_count": 7,
        "boundary_basis": [
            "사람이 갑자기 사라지거나 다시 나타나는 프레임 불연속마다 별도 컷으로 분리",
            "인물의 자세나 화면 위치가 연속 동작 없이 뚝 바뀌면 별도 컷으로 분리",
            "같은 안무 구간이어도 점프컷을 하나의 연속 장면으로 합치지 않음",
        ],
    },
    "doma_bad_challenge": {
        "status": "UPLOADED_JSON_REVIEWED",
        "expected_cut_count": 1,
        "boundary_basis": [
            "업로드 JSON이 0~11초 전체를 물리 컷 없는 단일 연속 촬영으로 명시",
            "0~4초 SETUP, 4~5초 BUILDUP, 5~11초 CLIMAX는 의미 구간이며 영상을 분할하지 않음",
            "5초 비트 드롭의 행동 반전은 인물 동작으로 만들고 합성 전환·속도 변경을 사용하지 않음",
        ],
    },
}


def _difficulty_label(source_lines: list[dict[str, Any]], challenge_id: str) -> str | None:
    """Return 하/중/상 from the shooting-difficulty stars in the analysis."""

    for row in source_lines:
        if row.get("guide_id") != challenge_id:
            continue
        line = str(row.get("raw_markdown_line") or "")
        if "난이도" not in line and "shooting_difficulty" not in line:
            continue
        match = re.search(r"(?:촬영|shooting_difficulty)[^★]{0,30}(★{1,3})", line)
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


def get_reference_cut_review(challenge_id: str) -> dict[str, Any] | None:
    review = _REFERENCE_CUT_REVIEWS.get(challenge_id)
    return copy.deepcopy(review) if review is not None else None


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
        source_guide_url = str(row.get("guide_youtube_url") or "").strip() or None
        guide_url = _SEED_GUIDE_URL_OVERRIDES.get(challenge_id, source_guide_url)
        generated = str(row.get("generated_at") or "").strip()
        if generated:
            generated_at.append(generated)
        result = {
            "id": challenge_id,
            "rank": int(rank),
            "name": str(row["name"]),
            "category": _SEED_CATEGORIES[challenge_id],
            "representative_youtube_url": guide_url,
            "guide_youtube_url": guide_url,
            **_video_format_metadata(source, str(row["id"])),
        }
        if challenge_id in _REFERENCE_CUT_REVIEWS:
            result["reference_cut_review"] = _REFERENCE_CUT_REVIEWS[challenge_id]
        results.append(result)
    results.sort(key=lambda item: (item["rank"], item["id"]))
    if len(results) != 4:
        raise ValueError(
            f"Expected exactly 4 PASS entries in the provided video-editing DB, got {len(results)}."
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
