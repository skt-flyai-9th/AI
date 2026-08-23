from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .utils import normalize_text, parse_aliases, parse_bool, parse_datetime, safe_float


CANDIDATE_COLUMNS = [
    "challenge_id",
    "name",
    "aliases",
    "category",
    "discovered_at",
    "kr_affinity_hint",
    "entity_confidence",
]

OBSERVATION_COLUMNS = [
    "challenge_id",
    "challenge_name",
    "platform",
    "content_id",
    "author_id",
    "created_at",
    "caption",
    "hashtags",
    "audio_id",
    "effect_id",
    "template_id",
    "views",
    "likes",
    "comments",
    "shares",
    "is_paid",
    "kr_affinity",
    "creator_followers",
    "creator_category",
]


def load_candidates(path: str | Path, timezone_name: str = "Asia/Seoul") -> pd.DataFrame:
    candidate_path = Path(path)
    if not candidate_path.exists():
        raise FileNotFoundError(
            f"후보 파일을 찾을 수 없습니다: {candidate_path}. "
            "자동 발굴 모드에서는 이 파일이 실행 중 생성되며, 수동 모드에서만 직접 준비해야 합니다."
        )
    frame = pd.read_csv(candidate_path, dtype=str).fillna("")
    missing = {"challenge_id", "name"} - set(frame.columns)
    if missing:
        raise ValueError(f"후보 CSV 필수 열이 없습니다: {sorted(missing)}")

    for column in CANDIDATE_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""

    frame["challenge_id"] = frame["challenge_id"].astype(str).str.strip()
    frame["name"] = frame["name"].astype(str).str.strip()
    if frame["challenge_id"].eq("").any() or frame["name"].eq("").any():
        raise ValueError("challenge_id와 name은 비어 있을 수 없습니다.")
    if frame["challenge_id"].duplicated().any():
        duplicates = frame.loc[frame["challenge_id"].duplicated(), "challenge_id"].tolist()
        raise ValueError(f"중복 challenge_id가 있습니다: {duplicates}")

    frame["alias_list"] = frame.apply(
        lambda row: parse_aliases(row.get("aliases"), fallback=row.get("name")), axis=1
    )
    frame["alias_norms"] = frame["alias_list"].apply(
        lambda items: [normalize_text(item) for item in items if normalize_text(item)]
    )
    frame["discovered_at"] = frame["discovered_at"].apply(
        lambda value: parse_datetime(value, default_tz=timezone_name)
    )
    frame["kr_affinity_hint"] = frame["kr_affinity_hint"].apply(
        lambda value: min(1.0, max(0.0, safe_float(value, 0.5)))
    )
    frame["entity_confidence"] = frame["entity_confidence"].apply(
        lambda value: min(1.0, max(0.0, safe_float(value, 0.7)))
    )
    return frame[CANDIDATE_COLUMNS + ["alias_list", "alias_norms"]]


def load_observations(
    path: str | Path,
    candidates: pd.DataFrame,
    timezone_name: str = "Asia/Seoul",
) -> pd.DataFrame:
    observation_path = Path(path)
    if not observation_path.exists():
        return pd.DataFrame(columns=OBSERVATION_COLUMNS)

    frame = pd.read_csv(observation_path, low_memory=False)
    for column in OBSERVATION_COLUMNS:
        if column not in frame.columns:
            frame[column] = np.nan

    frame["challenge_id"] = frame["challenge_id"].fillna("").astype(str).str.strip()
    frame["challenge_name"] = frame["challenge_name"].fillna("").astype(str).str.strip()
    _resolve_missing_challenge_ids(frame, candidates)

    valid_ids = set(candidates["challenge_id"])
    frame = frame[frame["challenge_id"].isin(valid_ids)].copy()
    if frame.empty:
        return pd.DataFrame(columns=OBSERVATION_COLUMNS)

    frame["created_at"] = frame["created_at"].apply(
        lambda value: parse_datetime(value, default_tz=timezone_name)
    )
    frame = frame[frame["created_at"].notna()].copy()

    for column in ("views", "likes", "comments", "shares", "creator_followers"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).clip(lower=0)
    frame["is_paid"] = frame["is_paid"].apply(parse_bool)
    frame["kr_affinity"] = pd.to_numeric(frame["kr_affinity"], errors="coerce").clip(0, 1)
    frame["platform"] = frame["platform"].fillna("unknown").astype(str).str.strip().replace("", "unknown")
    frame["author_id"] = frame["author_id"].fillna("").astype(str).str.strip()
    frame.loc[frame["author_id"].eq(""), "author_id"] = (
        "unknown:" + frame.index.astype(str)
    )
    frame["content_id"] = frame["content_id"].fillna("").astype(str).str.strip()
    frame.loc[frame["content_id"].eq(""), "content_id"] = (
        frame["platform"].astype(str) + ":" + frame.index.astype(str)
    )
    frame["creator_category"] = (
        frame["creator_category"].fillna("unknown").astype(str).str.strip().replace("", "unknown")
    )
    return frame[OBSERVATION_COLUMNS].copy()


def _resolve_missing_challenge_ids(frame: pd.DataFrame, candidates: pd.DataFrame) -> None:
    alias_map: dict[str, str] = {}
    for row in candidates.itertuples(index=False):
        for alias_norm in row.alias_norms:
            alias_map.setdefault(alias_norm, row.challenge_id)
        alias_map.setdefault(normalize_text(row.name), row.challenge_id)

    missing_mask = frame["challenge_id"].eq("")
    if not missing_mask.any():
        return

    resolved: list[str] = []
    for name in frame.loc[missing_mask, "challenge_name"]:
        norm = normalize_text(name)
        challenge_id = alias_map.get(norm, "")
        if not challenge_id and norm:
            matches = [cid for alias, cid in alias_map.items() if alias and (alias in norm or norm in alias)]
            challenge_id = matches[0] if len(set(matches)) == 1 else ""
        resolved.append(challenge_id)
    frame.loc[missing_mask, "challenge_id"] = resolved


def write_csv_atomic(frame: pd.DataFrame, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    frame.to_csv(temp_path, index=False, encoding="utf-8-sig")
    temp_path.replace(output_path)


def write_json_atomic(records: list[dict[str, Any]], path: str | Path) -> None:
    from .utils import json_dumps

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temp_path.write_text(json_dumps(records), encoding="utf-8")
    temp_path.replace(output_path)
