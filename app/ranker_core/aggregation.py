from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .utils import (
    clip01,
    normalized_entropy,
    safe_float,
    smoothed_growth,
    weighted_mean,
)


BASE_METRICS = [
    "creators_24h",
    "creators_prev24h",
    "creators_72h",
    "creators_prev72h",
    "creators_7d",
    "creators_prev7d",
    "posts_24h",
    "posts_prev24h",
    "posts_72h",
    "posts_prev72h",
    "posts_7d",
    "posts_prev7d",
    "views_7d",
    "engagements_7d",
    "engagement_rate_7d",
    "share_rate_7d",
    "adjusted_view_rate_7d",
    "creator_growth_24h",
    "creator_growth_72h",
    "post_growth_24h",
    "post_growth_72h",
    "platform_count_7d",
    "platform_entropy_7d",
    "category_entropy_7d",
    "tier_entropy_7d",
    "diversity_7d",
    "top10_creator_share_7d",
    "paid_ratio_7d",
    "organic_breadth_7d",
    "kr_affinity",
    "active_days_14",
    "persistence_14d",
    "age_days",
    "freshness",
    "sample_size",
    "evidence",
]


def aggregate_content_rows(
    candidates: pd.DataFrame,
    observations: pd.DataFrame,
    now: pd.Timestamp,
    *,
    prefix: str,
) -> pd.DataFrame:
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    else:
        now = now.tz_convert("UTC")

    frame = observations.copy()
    if not frame.empty:
        frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True, errors="coerce")
        frame = frame[frame["created_at"].notna() & (frame["created_at"] <= now)].copy()
        _ensure_columns(frame)

    records: list[dict[str, Any]] = []
    for candidate in candidates.itertuples(index=False):
        if frame.empty:
            group = frame
        else:
            group = frame[frame["challenge_id"] == candidate.challenge_id].copy()
        record = {"challenge_id": candidate.challenge_id}
        metrics = _aggregate_candidate_group(group, candidate, now)
        record.update({f"{prefix}_{name}": value for name, value in metrics.items()})
        records.append(record)

    columns = ["challenge_id"] + [f"{prefix}_{name}" for name in BASE_METRICS]
    result = pd.DataFrame(records)
    for column in columns:
        if column not in result.columns:
            result[column] = 0.0
    return result[columns]


def _aggregate_candidate_group(
    group: pd.DataFrame,
    candidate: Any,
    now: pd.Timestamp,
) -> dict[str, float]:
    discovered_at = getattr(candidate, "discovered_at", pd.NaT)
    kr_hint = safe_float(getattr(candidate, "kr_affinity_hint", 0.5), 0.5)

    if group.empty:
        age_days = _age_days(discovered_at, now, fallback=365.0)
        return _empty_metrics(age_days, kr_hint)

    cur24 = _between(group, now - pd.Timedelta(hours=24), now)
    prev24 = _between(group, now - pd.Timedelta(hours=48), now - pd.Timedelta(hours=24))
    cur72 = _between(group, now - pd.Timedelta(hours=72), now)
    prev72 = _between(group, now - pd.Timedelta(hours=144), now - pd.Timedelta(hours=72))
    cur7 = _between(group, now - pd.Timedelta(days=7), now)
    prev7 = _between(group, now - pd.Timedelta(days=14), now - pd.Timedelta(days=7))
    cur14 = _between(group, now - pd.Timedelta(days=14), now)

    creators_24h = float(cur24["author_id"].nunique())
    creators_prev24h = float(prev24["author_id"].nunique())
    creators_72h = float(cur72["author_id"].nunique())
    creators_prev72h = float(prev72["author_id"].nunique())
    creators_7d = float(cur7["author_id"].nunique())
    creators_prev7d = float(prev7["author_id"].nunique())

    posts_24h = float(cur24["content_id"].nunique())
    posts_prev24h = float(prev24["content_id"].nunique())
    posts_72h = float(cur72["content_id"].nunique())
    posts_prev72h = float(prev72["content_id"].nunique())
    posts_7d = float(cur7["content_id"].nunique())
    posts_prev7d = float(prev7["content_id"].nunique())

    views_7d = float(cur7["views"].sum())
    engagements_7d = float(cur7[["likes", "comments", "shares"]].sum().sum())
    engagement_rate = engagements_7d / max(1.0, views_7d)
    share_rate = float(cur7["shares"].sum()) / max(1.0, views_7d)

    adjusted_rates = cur7["views"] / (cur7["creator_followers"] + 1000.0)
    adjusted_view_rate = float(adjusted_rates.replace([np.inf, -np.inf], np.nan).median())
    if not np.isfinite(adjusted_view_rate):
        adjusted_view_rate = 0.0

    platform_count = float(cur7["platform"].nunique())
    weights = np.sqrt(cur7["views"].astype(float) + 1.0).tolist()
    platform_entropy = normalized_entropy(cur7["platform"].tolist(), weights)
    category_entropy = normalized_entropy(cur7["creator_category"].tolist(), weights)
    tiers = [_creator_tier(value) for value in cur7["creator_followers"]]
    tier_entropy = normalized_entropy(tiers, weights)
    diversity = float(np.mean([platform_entropy, category_entropy, tier_entropy]))

    top10_share = _top_creator_share(cur7)
    paid_ratio = float(cur7["is_paid"].astype(float).mean()) if not cur7.empty else 0.0
    organic_breadth = clip01((1.0 - paid_ratio) * (1.0 - top10_share))

    kr_values: list[tuple[float | None, float]] = []
    for row in cur7.itertuples(index=False):
        value = getattr(row, "kr_affinity", np.nan)
        if pd.isna(value):
            continue
        kr_values.append((safe_float(value), math.sqrt(safe_float(getattr(row, "views", 0)) + 1.0)))
    observed_kr = weighted_mean(kr_values)
    kr_affinity = clip01(0.75 * observed_kr + 0.25 * kr_hint) if kr_values else clip01(kr_hint)

    active_days_14 = float(cur14["created_at"].dt.floor("D").nunique())
    balance = min(creators_7d, creators_prev7d) / max(1.0, max(creators_7d, creators_prev7d))
    active_ratio = active_days_14 / 14.0
    persistence = clip01(0.65 * active_ratio + 0.35 * balance)

    first_observed = group["created_at"].min()
    first_seen = _earliest_timestamp(discovered_at, first_observed)
    age_days = _age_days(first_seen, now, fallback=365.0)
    freshness = float(math.exp(-max(0.0, age_days) / 21.0))

    return {
        "creators_24h": creators_24h,
        "creators_prev24h": creators_prev24h,
        "creators_72h": creators_72h,
        "creators_prev72h": creators_prev72h,
        "creators_7d": creators_7d,
        "creators_prev7d": creators_prev7d,
        "posts_24h": posts_24h,
        "posts_prev24h": posts_prev24h,
        "posts_72h": posts_72h,
        "posts_prev72h": posts_prev72h,
        "posts_7d": posts_7d,
        "posts_prev7d": posts_prev7d,
        "views_7d": views_7d,
        "engagements_7d": engagements_7d,
        "engagement_rate_7d": engagement_rate,
        "share_rate_7d": share_rate,
        "adjusted_view_rate_7d": adjusted_view_rate,
        "creator_growth_24h": smoothed_growth(creators_24h, creators_prev24h),
        "creator_growth_72h": smoothed_growth(creators_72h, creators_prev72h),
        "post_growth_24h": smoothed_growth(posts_24h, posts_prev24h),
        "post_growth_72h": smoothed_growth(posts_72h, posts_prev72h),
        "platform_count_7d": platform_count,
        "platform_entropy_7d": platform_entropy,
        "category_entropy_7d": category_entropy,
        "tier_entropy_7d": tier_entropy,
        "diversity_7d": diversity,
        "top10_creator_share_7d": top10_share,
        "paid_ratio_7d": paid_ratio,
        "organic_breadth_7d": organic_breadth,
        "kr_affinity": kr_affinity,
        "active_days_14": active_days_14,
        "persistence_14d": persistence,
        "age_days": age_days,
        "freshness": freshness,
        "sample_size": posts_7d,
        "evidence": 1.0 if posts_7d > 0 else 0.0,
    }


def _ensure_columns(frame: pd.DataFrame) -> None:
    defaults: dict[str, Any] = {
        "platform": "unknown",
        "content_id": "",
        "author_id": "",
        "views": 0.0,
        "likes": 0.0,
        "comments": 0.0,
        "shares": 0.0,
        "is_paid": False,
        "kr_affinity": np.nan,
        "creator_followers": 0.0,
        "creator_category": "unknown",
    }
    for column, default in defaults.items():
        if column not in frame.columns:
            frame[column] = default
    for column in ("views", "likes", "comments", "shares", "creator_followers"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0).clip(lower=0)
    frame["is_paid"] = frame["is_paid"].fillna(False).astype(bool)
    frame["platform"] = frame["platform"].fillna("unknown").astype(str)
    frame["creator_category"] = frame["creator_category"].fillna("unknown").astype(str)
    frame["author_id"] = frame["author_id"].fillna("").astype(str)
    frame["content_id"] = frame["content_id"].fillna("").astype(str)


def _between(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return frame[(frame["created_at"] >= start) & (frame["created_at"] < end)]


def _creator_tier(followers: Any) -> str:
    value = safe_float(followers)
    if value < 10_000:
        return "nano"
    if value < 100_000:
        return "micro"
    if value < 1_000_000:
        return "mid"
    return "macro"


def _top_creator_share(frame: pd.DataFrame, top_n: int = 10) -> float:
    if frame.empty:
        return 0.0
    by_author = frame.groupby("author_id", dropna=False)["views"].sum()
    total = float(by_author.sum())
    if total <= 0:
        by_author = frame.groupby("author_id", dropna=False)["content_id"].count().astype(float)
        total = float(by_author.sum())
    if total <= 0:
        return 0.0
    return clip01(float(by_author.nlargest(top_n).sum()) / total)


def _earliest_timestamp(*values: Any) -> pd.Timestamp | pd.NaT:
    timestamps = [pd.Timestamp(value) for value in values if value is not None and not pd.isna(value)]
    if not timestamps:
        return pd.NaT
    normalized = [ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC") for ts in timestamps]
    return min(normalized)


def _age_days(value: Any, now: pd.Timestamp, fallback: float) -> float:
    if value is None or pd.isna(value):
        return fallback
    ts = pd.Timestamp(value)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return max(0.0, float((now - ts).total_seconds() / 86400.0))


def _empty_metrics(age_days: float, kr_hint: float) -> dict[str, float]:
    metrics = {name: 0.0 for name in BASE_METRICS}
    metrics["creator_growth_24h"] = 0.0
    metrics["creator_growth_72h"] = 0.0
    metrics["post_growth_24h"] = 0.0
    metrics["post_growth_72h"] = 0.0
    metrics["kr_affinity"] = clip01(kr_hint)
    metrics["age_days"] = age_days
    metrics["freshness"] = float(math.exp(-max(0.0, age_days) / 21.0))
    return metrics
