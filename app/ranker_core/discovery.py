from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .io import OBSERVATION_COLUMNS, write_csv_atomic
from .utils import (
    clip01,
    has_korean,
    normalize_text,
    parse_datetime,
    safe_float,
    stable_hash,
)


_GENERIC_HASHTAGS = {
    "fyp",
    "foryou",
    "foryoupage",
    "viral",
    "reels",
    "reel",
    "shorts",
    "short",
    "추천",
    "일상",
    "챌린지",
    "challenge",
    "인스타",
    "instagram",
    "릴스",
}
_HASHTAG_RE = re.compile(r"#([0-9A-Za-z가-힣_]+)")
_SPLIT_RE = re.compile(r"[|,;\s]+")


def discover_candidates_from_csv(
    observations_path: str | Path,
    candidates_output: str | Path,
    *,
    resolved_observations_output: str | Path | None = None,
    min_posts: int = 3,
    min_authors: int = 3,
    timezone_name: str = "Asia/Seoul",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    input_path = Path(observations_path)
    if not input_path.exists():
        raise FileNotFoundError(f"관측 CSV를 찾을 수 없습니다: {input_path}")

    frame = pd.read_csv(input_path, low_memory=False)
    for column in OBSERVATION_COLUMNS:
        if column not in frame.columns:
            frame[column] = np.nan
    frame = frame[OBSERVATION_COLUMNS].copy()
    frame["challenge_id"] = frame["challenge_id"].fillna("").astype(str).str.strip()
    frame["challenge_name"] = frame["challenge_name"].fillna("").astype(str).str.strip()
    frame["platform"] = frame["platform"].fillna("unknown").astype(str).str.strip().replace("", "unknown")
    frame["content_id"] = frame["content_id"].fillna("").astype(str).str.strip()
    frame.loc[frame["content_id"].eq(""), "content_id"] = "row:" + frame.index.astype(str)
    frame["author_id"] = frame["author_id"].fillna("").astype(str).str.strip()
    frame.loc[frame["author_id"].eq(""), "author_id"] = "unknown:" + frame.index.astype(str)
    frame["created_at"] = frame["created_at"].apply(
        lambda value: parse_datetime(value, default_tz=timezone_name)
    )
    frame["kr_affinity"] = pd.to_numeric(frame["kr_affinity"], errors="coerce").clip(0, 1)

    keys: list[str] = []
    bases: list[float] = []
    labels: list[str] = []
    for row in frame.to_dict(orient="records"):
        key, base_confidence, label = _entity_key(row)
        keys.append(key)
        bases.append(base_confidence)
        labels.append(label)
    frame["_entity_key"] = keys
    frame["_base_confidence"] = bases
    frame["_entity_label"] = labels
    frame = frame[frame["_entity_key"].ne("")].copy()

    candidates: list[dict[str, Any]] = []
    resolved_parts: list[pd.DataFrame] = []
    for entity_key, group in frame.groupby("_entity_key", sort=False):
        posts = int(group["content_id"].nunique())
        authors = int(group["author_id"].nunique())
        if posts < max(1, min_posts) or authors < max(1, min_authors):
            continue

        explicit_ids = [value for value in group["challenge_id"].unique() if str(value).strip()]
        challenge_id = explicit_ids[0] if len(explicit_ids) == 1 else f"auto_{stable_hash(entity_key, 14)}"
        name = _candidate_name(group)
        aliases = _candidate_aliases(group, name)
        discovered_at = group["created_at"].dropna().min()
        category = _mode_nonempty(group["creator_category"], default="unknown")
        kr_hint = _kr_affinity_hint(group, name)
        base_confidence = float(group["_base_confidence"].max())
        sample_bonus = min(0.10, math.log1p(authors) / 35.0 + math.log1p(posts) / 60.0)
        entity_confidence = clip01(base_confidence + sample_bonus)

        candidates.append(
            {
                "challenge_id": challenge_id,
                "name": name,
                "aliases": "|".join(aliases),
                "category": category,
                "discovered_at": discovered_at.isoformat() if not pd.isna(discovered_at) else "",
                "kr_affinity_hint": round(kr_hint, 4),
                "entity_confidence": round(entity_confidence, 4),
                "discovery_posts": posts,
                "discovery_authors": authors,
                "discovery_key": entity_key,
            }
        )
        resolved = group.drop(columns=["_entity_key", "_base_confidence", "_entity_label"]).copy()
        resolved["challenge_id"] = challenge_id
        resolved_parts.append(resolved)

    candidate_frame = pd.DataFrame(candidates)
    if candidate_frame.empty:
        candidate_frame = pd.DataFrame(
            columns=[
                "challenge_id",
                "name",
                "aliases",
                "category",
                "discovered_at",
                "kr_affinity_hint",
                "entity_confidence",
                "discovery_posts",
                "discovery_authors",
                "discovery_key",
            ]
        )
        resolved_frame = pd.DataFrame(columns=OBSERVATION_COLUMNS)
    else:
        candidate_frame = candidate_frame.sort_values(
            ["discovery_authors", "discovery_posts"], ascending=False
        ).reset_index(drop=True)
        resolved_frame = pd.concat(resolved_parts, ignore_index=True)
        resolved_frame = resolved_frame[OBSERVATION_COLUMNS]

    write_csv_atomic(candidate_frame, candidates_output)
    if resolved_observations_output is not None:
        write_csv_atomic(resolved_frame, resolved_observations_output)
    return candidate_frame, resolved_frame


def _entity_key(row: dict[str, Any]) -> tuple[str, float, str]:
    explicit_id = str(row.get("challenge_id") or "").strip()
    if explicit_id:
        return f"id:{explicit_id}", 0.93, explicit_id

    challenge_name = str(row.get("challenge_name") or "").strip()
    name_norm = normalize_text(challenge_name)
    if name_norm:
        return f"name:{name_norm}", 0.86, challenge_name

    platform = normalize_text(row.get("platform")) or "unknown"
    template_id = str(row.get("template_id") or "").strip()
    if template_id and template_id.lower() != "nan":
        return f"template:{platform}:{template_id}", 0.82, f"template:{template_id}"

    effect_id = _first_identifier(row.get("effect_id"))
    if effect_id:
        return f"effect:{platform}:{effect_id}", 0.76, f"effect:{effect_id}"

    audio_id = str(row.get("audio_id") or "").strip()
    if audio_id.lower() == "nan":
        audio_id = ""
    hashtags = _hashtags(row)
    if audio_id and hashtags:
        return f"audio_tag:{platform}:{audio_id}:{hashtags[0]}", 0.72, f"#{hashtags[0]}"
    if audio_id:
        return f"audio:{platform}:{audio_id}", 0.56, f"audio:{audio_id}"
    if hashtags:
        return f"hashtag:{hashtags[0]}", 0.60, f"#{hashtags[0]}"
    return "", 0.0, ""


def _candidate_name(group: pd.DataFrame) -> str:
    named = [str(value).strip() for value in group["challenge_name"] if str(value).strip()]
    if named:
        return pd.Series(named).mode().iloc[0]
    labels = [str(value).strip() for value in group["_entity_label"] if str(value).strip()]
    if labels:
        return pd.Series(labels).mode().iloc[0]
    return "자동 발견 챌린지"


def _candidate_aliases(group: pd.DataFrame, name: str) -> list[str]:
    aliases: list[str] = [name]
    aliases.extend(
        str(value).strip() for value in group["challenge_name"] if str(value).strip()
    )
    hashtag_counts: dict[str, int] = {}
    for row in group.to_dict(orient="records"):
        for hashtag in _hashtags(row):
            hashtag_counts[hashtag] = hashtag_counts.get(hashtag, 0) + 1
    aliases.extend(f"#{tag}" for tag, _ in sorted(hashtag_counts.items(), key=lambda item: -item[1])[:8])

    result: list[str] = []
    seen: set[str] = set()
    for alias in aliases:
        norm = normalize_text(alias)
        if norm and norm not in seen:
            seen.add(norm)
            result.append(alias)
        if len(result) >= 12:
            break
    return result


def _hashtags(row: dict[str, Any]) -> list[str]:
    text = " ".join(
        [
            str(row.get("hashtags") or ""),
            str(row.get("caption") or ""),
        ]
    )
    tags = _HASHTAG_RE.findall(text)
    if not tags:
        tags = [part.lstrip("#") for part in _SPLIT_RE.split(str(row.get("hashtags") or ""))]
    result: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        normalized = normalize_text(tag).replace(" ", "")
        if not normalized or normalized in _GENERIC_HASHTAGS or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _first_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return ""
    return _SPLIT_RE.split(text)[0]


def _mode_nonempty(series: pd.Series, default: str) -> str:
    values = [str(value).strip() for value in series if str(value).strip() and str(value) != "nan"]
    return pd.Series(values).mode().iloc[0] if values else default


def _kr_affinity_hint(group: pd.DataFrame, name: str) -> float:
    values = group["kr_affinity"].dropna().astype(float)
    if len(values):
        return clip01(float(values.mean()))
    text = " ".join([name] + [str(value) for value in group["caption"].head(20)])
    return 0.8 if has_korean(text) else 0.5
