from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .utils import clip01, safe_float, weighted_mean


CONTENT_PREFIXES = ("instagram", "local", "youtube")


def build_features(
    candidates: pd.DataFrame,
    merged_metrics: pd.DataFrame,
    statuses: dict[str, dict[str, Any]],
    now: pd.Timestamp,
) -> pd.DataFrame:
    base_columns = [
        "challenge_id",
        "name",
        "category",
        "discovered_at",
        "kr_affinity_hint",
        "entity_confidence",
    ]
    frame = candidates[base_columns].merge(merged_metrics, on="challenge_id", how="left")

    enabled_count = sum(1 for status in statuses.values() if status.get("enabled", False))
    success_count = sum(1 for status in statuses.values() if status.get("success", False))
    coverage_ratio = success_count / max(1, enabled_count)

    records: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        content_evidence = {
            prefix: _get(row, f"{prefix}_evidence") for prefix in CONTENT_PREFIXES
        }
        x_evidence = _get(row, "x_evidence")
        search_evidence = _get(row, "naver_search_evidence")
        blog_evidence = _get(row, "naver_blog_evidence")
        news_evidence = _get(row, "naver_news_evidence")
        evidence_values = list(content_evidence.values()) + [
            x_evidence,
            search_evidence,
            blog_evidence,
            news_evidence,
        ]
        evidence_count = float(sum(1 for value in evidence_values if value > 0))

        participation_acceleration = weighted_mean(
            [
                (_maybe(row, "instagram_creator_growth_24h", content_evidence["instagram"]), 1.35),
                (_maybe(row, "instagram_post_growth_24h", content_evidence["instagram"]), 0.70),
                (_maybe(row, "local_creator_growth_24h", content_evidence["local"]), 1.00),
                (_maybe(row, "local_post_growth_24h", content_evidence["local"]), 0.50),
                (_maybe(row, "youtube_creator_growth_24h", content_evidence["youtube"]), 0.45),
                (_maybe(row, "youtube_post_growth_24h", content_evidence["youtube"]), 0.25),
                (_maybe(row, "x_post_growth_24h", x_evidence), 0.30),
                (_maybe(row, "x_post_growth_72h", x_evidence), 0.25),
            ]
        )

        kr_creator_growth = weighted_mean(
            [
                (
                    _scaled_growth(
                        _maybe(row, "instagram_creator_growth_24h", content_evidence["instagram"]),
                        _get(row, "instagram_kr_affinity"),
                    ),
                    1.20,
                ),
                (
                    _scaled_growth(
                        _maybe(row, "local_creator_growth_24h", content_evidence["local"]),
                        _get(row, "local_kr_affinity"),
                    ),
                    1.00,
                ),
                (
                    _scaled_growth(
                        _maybe(row, "youtube_creator_growth_24h", content_evidence["youtube"]),
                        _get(row, "youtube_kr_affinity"),
                    ),
                    0.55,
                ),
            ]
        )

        unique_creators_7d = sum(_get(row, f"{prefix}_creators_7d") for prefix in CONTENT_PREFIXES)
        posts_7d = sum(_get(row, f"{prefix}_posts_7d") for prefix in CONTENT_PREFIXES)
        views_7d = sum(_get(row, f"{prefix}_views_7d") for prefix in CONTENT_PREFIXES)
        engagements_7d = sum(_get(row, f"{prefix}_engagements_7d") for prefix in CONTENT_PREFIXES)
        adjusted_reach = sum(
            _get(row, f"{prefix}_adjusted_view_rate_7d") for prefix in CONTENT_PREFIXES
        )

        content_weights = [
            (_get(row, f"{prefix}_diversity_7d"), max(0.0, _get(row, f"{prefix}_sample_size")))
            for prefix in CONTENT_PREFIXES
        ]
        creator_diversity = weighted_mean(content_weights)

        local_platform_count = _get(row, "local_platform_count_7d")
        api_platform_add = (
            (1.0 if content_evidence["instagram"] > 0 else 0.0)
            + (1.0 if content_evidence["youtube"] > 0 else 0.0)
            + (1.0 if x_evidence > 0 else 0.0)
            + (1.0 if blog_evidence > 0 else 0.0)
        )
        cross_platform_count = min(
            6.0,
            local_platform_count + api_platform_add if content_evidence["local"] > 0 else api_platform_add,
        )
        if creator_diversity <= 0 and cross_platform_count > 1:
            creator_diversity = clip01((cross_platform_count - 1.0) / 4.0)

        share_rate = weighted_mean(
            [
                (
                    _maybe(row, f"{prefix}_share_rate_7d", content_evidence[prefix]),
                    max(1.0, _get(row, f"{prefix}_views_7d")),
                )
                for prefix in CONTENT_PREFIXES
            ]
        )
        engagement_rate = engagements_7d / max(1.0, views_7d)
        creator_per_100k_views = unique_creators_7d / max(1.0, views_7d / 100_000.0)
        audio_reuse = _get(row, "instagram_audio_reuse_ratio")
        participation_conversion = share_rate + 0.0005 * creator_per_100k_views + 0.02 * audio_reuse

        persistence = weighted_mean(
            [
                (
                    _maybe(row, f"{prefix}_persistence_14d", content_evidence[prefix]),
                    max(1.0, _get(row, f"{prefix}_sample_size")),
                )
                for prefix in CONTENT_PREFIXES
            ]
        )

        paid_ratio = max(_get(row, f"{prefix}_paid_ratio_7d") for prefix in CONTENT_PREFIXES)
        concentration = max(
            _get(row, f"{prefix}_top10_creator_share_7d") for prefix in CONTENT_PREFIXES
        )
        organic_breadth = weighted_mean(
            [
                (
                    _maybe(row, f"{prefix}_organic_breadth_7d", content_evidence[prefix]),
                    max(1.0, _get(row, f"{prefix}_sample_size")),
                )
                for prefix in CONTENT_PREFIXES
            ]
        )
        seeded_penalty = clip01(0.60 * paid_ratio + 0.40 * concentration)

        kr_affinity = weighted_mean(
            [
                (safe_float(row.get("kr_affinity_hint"), 0.5), 0.50),
                (_maybe(row, "instagram_kr_affinity", content_evidence["instagram"]), 1.05),
                (_maybe(row, "local_kr_affinity", content_evidence["local"]), 1.00),
                (_maybe(row, "youtube_kr_affinity", content_evidence["youtube"]), 0.55),
                (1.0 if search_evidence > 0 else None, 0.55),
                (1.0 if blog_evidence > 0 else None, 0.40),
                (1.0 if news_evidence > 0 else None, 0.25),
            ]
        )
        kr_affinity = clip01(kr_affinity)

        age_days = _challenge_age_days(row, now, content_evidence)
        freshness = float(math.exp(-max(0.0, age_days) / 21.0))

        naver_search_lift = _get(row, "naver_search_lift_3d")
        naver_search_acceleration = _get(row, "naver_search_acceleration")
        blog_7d = _get(row, "naver_blog_7d")
        blog_growth = _get(row, "naver_blog_growth_7d")
        blog_authors_7d = _get(row, "naver_blog_authors_7d")
        news_7d = _get(row, "naver_news_7d")
        news_growth = _get(row, "naver_news_growth_7d")
        x_posts_7d = _get(row, "x_posts_7d")
        spillover = 0.45 * blog_7d + news_7d + 0.10 * x_posts_7d

        sample_size = (
            posts_7d
            + min(100.0, x_posts_7d)
            + min(100.0, blog_7d)
            + min(30.0, _get(row, "naver_search_sample_days"))
        )
        sample_confidence = 1.0 - math.exp(-sample_size / 40.0)
        evidence_confidence = evidence_count / max(1.0, float(enabled_count))
        entity_confidence = clip01(safe_float(row.get("entity_confidence"), 0.7))
        kr_signal_confidence = clip01(
            0.45 * kr_affinity
            + 0.25 * (1.0 if search_evidence > 0 else 0.0)
            + 0.15 * (1.0 if content_evidence["instagram"] > 0 else 0.0)
            + 0.10 * (1.0 if content_evidence["local"] > 0 else 0.0)
            + 0.07 * (1.0 if content_evidence["youtube"] > 0 else 0.0)
            + 0.05 * (1.0 if blog_evidence > 0 else 0.0)
        )
        confidence = 100.0 * clip01(
            0.30 * coverage_ratio
            + 0.20 * evidence_confidence
            + 0.20 * sample_confidence
            + 0.15 * entity_confidence
            + 0.15 * kr_signal_confidence
        )

        records.append(
            {
                "challenge_id": row["challenge_id"],
                "name": row["name"],
                "category": row.get("category", ""),
                "discovered_at": row.get("discovered_at"),
                "participation_acceleration": participation_acceleration,
                "kr_creator_growth": kr_creator_growth,
                "unique_creators_7d": unique_creators_7d,
                "posts_7d": posts_7d,
                "views_7d": views_7d,
                "engagement_rate_7d": engagement_rate,
                "adjusted_reach": adjusted_reach,
                "creator_diversity": clip01(creator_diversity),
                "cross_platform_count": cross_platform_count,
                "search_lift": naver_search_lift,
                "search_acceleration": naver_search_acceleration,
                "participation_conversion": max(0.0, participation_conversion),
                "instagram_audio_reuse_ratio": audio_reuse,
                "persistence": clip01(persistence),
                "organic_breadth": clip01(organic_breadth),
                "paid_ratio": clip01(paid_ratio),
                "creator_concentration": clip01(concentration),
                "seeded_penalty": seeded_penalty,
                "kr_affinity": kr_affinity,
                "age_days": age_days,
                "freshness": freshness,
                "blog_7d": blog_7d,
                "blog_growth": blog_growth,
                "blog_authors_7d": blog_authors_7d,
                "news_7d": news_7d,
                "news_growth": news_growth,
                "x_posts_7d": x_posts_7d,
                "spillover": spillover,
                "sample_size": sample_size,
                "evidence_source_count": evidence_count,
                "source_coverage_ratio": coverage_ratio,
                "confidence": confidence,
            }
        )

    return pd.DataFrame(records)


def _get(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get(key, default)
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _maybe(row: dict[str, Any], key: str, evidence: float) -> float | None:
    return _get(row, key) if evidence > 0 else None


def _challenge_age_days(
    row: dict[str, Any], now: pd.Timestamp, evidence: dict[str, float]
) -> float:
    ages: list[float] = []
    discovered = row.get("discovered_at")
    if discovered is not None and not pd.isna(discovered):
        ts = pd.Timestamp(discovered)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        ages.append(max(0.0, (now - ts).total_seconds() / 86400.0))
    for prefix, present in evidence.items():
        if present > 0:
            ages.append(max(0.0, _get(row, f"{prefix}_age_days")))
    return max(ages) if ages else 365.0


def _scaled_growth(value: float | None, kr_affinity: float) -> float | None:
    if value is None:
        return None
    return float(value) * clip01(kr_affinity)
