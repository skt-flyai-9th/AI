from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np
import pandas as pd


DomainFn = Callable[[float], float]


def score_challenges(features: pd.DataFrame, ranking_config: dict[str, Any]) -> pd.DataFrame:
    if features.empty:
        return features.copy()

    frame = features.copy()
    frame["reach_signal"] = np.log1p(frame["views_7d"].clip(lower=0)) + 1.5 * np.log1p(
        frame["adjusted_reach"].clip(lower=0)
    )

    components: dict[str, pd.Series] = {
        "participation_acceleration_score": _hybrid_score(
            frame["participation_acceleration"], _signed_growth_domain
        ),
        "kr_creator_growth_score": _hybrid_score(frame["kr_creator_growth"], _signed_growth_domain),
        "creator_diversity_score": _hybrid_score(frame["creator_diversity"], _unit_domain),
        "cross_platform_score": _hybrid_score(
            frame["cross_platform_count"], lambda value: _count_domain(value, scale=4.0)
        ),
        "search_lift_score": _hybrid_score(frame["search_lift"], _signed_growth_domain),
        "search_acceleration_score": _hybrid_score(
            frame["search_acceleration"], _signed_growth_domain
        ),
        "freshness_score": _hybrid_score(frame["freshness"], _unit_domain),
        "participation_conversion_score": _hybrid_score(
            frame["participation_conversion"], lambda value: _rate_domain(value, scale=0.012)
        ),
        "unique_creators_score": _hybrid_score(
            frame["unique_creators_7d"], lambda value: _count_domain(value, scale=100.0)
        ),
        "reach_score": _hybrid_score(
            frame["reach_signal"], lambda value: _linear_domain(value, scale=18.0)
        ),
        "persistence_score": _hybrid_score(frame["persistence"], _unit_domain),
        "spillover_score": _hybrid_score(
            frame["spillover"], lambda value: _count_domain(value, scale=60.0)
        ),
        "organic_breadth_score": _hybrid_score(frame["organic_breadth"], _unit_domain),
    }
    for name, values in components.items():
        frame[name] = values

    emerging_weights = _normalized_weights(ranking_config.get("emerging_weights", {}))
    mainstream_weights = _normalized_weights(ranking_config.get("mainstream_weights", {}))

    emerging_map = {
        "participation_acceleration": "participation_acceleration_score",
        "kr_creator_growth": "kr_creator_growth_score",
        "creator_diversity": "creator_diversity_score",
        "cross_platform": "cross_platform_score",
        "search_lift": "search_lift_score",
        "freshness": "freshness_score",
        "participation_conversion": "participation_conversion_score",
    }
    mainstream_map = {
        "unique_creators": "unique_creators_score",
        "reach": "reach_score",
        "persistence": "persistence_score",
        "cross_platform": "cross_platform_score",
        "spillover": "spillover_score",
        "creator_diversity": "creator_diversity_score",
        "organic_breadth": "organic_breadth_score",
    }

    frame["emerging_pre_confidence"] = _weighted_components(frame, emerging_weights, emerging_map)
    penalty_max = float(ranking_config.get("seeded_penalty_max", 15.0))
    frame["emerging_pre_confidence"] = (
        frame["emerging_pre_confidence"] - penalty_max * frame["seeded_penalty"].clip(0, 1)
    ).clip(0, 100)
    frame["mainstream_pre_confidence"] = _weighted_components(
        frame, mainstream_weights, mainstream_map
    ).clip(0, 100)

    floor = min(1.0, max(0.0, float(ranking_config.get("confidence_floor_multiplier", 0.60))))
    multiplier = floor + (1.0 - floor) * frame["confidence"].clip(0, 100) / 100.0
    frame["emerging_score"] = (frame["emerging_pre_confidence"] * multiplier).clip(0, 100)
    frame["mainstream_score"] = (frame["mainstream_pre_confidence"] * multiplier).clip(0, 100)

    decline_score = 100.0 - frame["participation_acceleration_score"]
    search_decline_score = 100.0 - frame["search_acceleration_score"]
    age_score = 100.0 - frame["freshness_score"]
    concentration_score = 100.0 * frame["creator_concentration"].clip(0, 1)
    frame["saturation_score"] = (
        0.35 * decline_score
        + 0.15 * search_decline_score
        + 0.20 * age_score
        + 0.20 * frame["mainstream_pre_confidence"]
        + 0.10 * concentration_score
    ).clip(0, 100)

    frame["domestic_multiplier"] = 0.45 + 0.55 * frame["kr_affinity"].clip(0, 1)
    frame["final_score"] = (
        (
            0.58 * frame["emerging_score"]
            + 0.32 * frame["mainstream_score"]
            + 0.10 * frame["confidence"]
            - 0.22 * frame["saturation_score"]
        )
        * frame["domestic_multiplier"]
    ).clip(0, 100)

    frame["stage"] = frame.apply(_stage, axis=1)
    frame["recommended_action"] = frame["stage"].map(
        {
            "Emerging": "선점 검토",
            "Rising": "지금 참여",
            "Peak": "빠른 변주로 참여",
            "Saturating": "강한 차별화 없으면 보류",
            "Declining": "신규 참여 비추천",
            "Watching": "관찰 유지",
        }
    )

    frame["final_rank"] = _rank(frame["final_score"])
    frame["emerging_rank"] = _rank(frame["emerging_score"])
    frame["mainstream_rank"] = _rank(frame["mainstream_score"])
    frame["saturation_rank"] = _rank(frame["saturation_score"], ascending=False)

    return frame.sort_values(["final_rank", "confidence"], ascending=[True, False]).reset_index(drop=True)


def _hybrid_score(series: pd.Series, domain_fn: DomainFn) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce").fillna(0.0).astype(float)
    domain = numeric.map(lambda value: float(np.clip(domain_fn(float(value)), 0.0, 100.0)))
    if len(numeric) < 3 or numeric.nunique(dropna=False) <= 1:
        return domain

    ranks = numeric.rank(method="average", ascending=True)
    relative = (ranks - 1.0) / max(1.0, float(len(numeric) - 1)) * 100.0
    return (0.55 * relative + 0.45 * domain).clip(0, 100)


def _signed_growth_domain(value: float) -> float:
    return 50.0 + 50.0 * math.tanh(value / 1.25)


def _unit_domain(value: float) -> float:
    return 100.0 * float(np.clip(value, 0.0, 1.0))


def _count_domain(value: float, scale: float) -> float:
    return 100.0 * (1.0 - math.exp(-max(0.0, value) / max(1e-9, scale)))


def _rate_domain(value: float, scale: float) -> float:
    return 100.0 * (1.0 - math.exp(-max(0.0, value) / max(1e-9, scale)))


def _linear_domain(value: float, scale: float) -> float:
    return 100.0 * float(np.clip(max(0.0, value) / max(1e-9, scale), 0.0, 1.0))


def _normalized_weights(weights: dict[str, Any]) -> dict[str, float]:
    numeric = {key: max(0.0, float(value)) for key, value in weights.items()}
    total = sum(numeric.values())
    if total <= 0:
        raise ValueError("랭킹 가중치 합은 0보다 커야 합니다.")
    return {key: value / total for key, value in numeric.items()}


def _weighted_components(
    frame: pd.DataFrame,
    weights: dict[str, float],
    mapping: dict[str, str],
) -> pd.Series:
    result = pd.Series(0.0, index=frame.index)
    used_weight = 0.0
    for key, weight in weights.items():
        column = mapping.get(key)
        if not column or column not in frame.columns:
            continue
        result = result + weight * frame[column]
        used_weight += weight
    if used_weight <= 0:
        return result
    return result / used_weight


def _stage(row: pd.Series) -> str:
    acceleration = float(row["participation_acceleration_score"])
    emerging = float(row["emerging_score"])
    mainstream = float(row["mainstream_score"])
    saturation = float(row["saturation_score"])
    age_days = float(row["age_days"])

    if saturation >= 72 and acceleration < 42:
        return "Declining" if mainstream < 58 else "Saturating"
    if mainstream >= 72 and saturation < 67:
        return "Peak"
    if emerging >= 70 and age_days <= 14:
        return "Emerging"
    if emerging >= 62 and acceleration >= 55:
        return "Rising"
    if mainstream >= 62:
        return "Peak"
    if saturation >= 62:
        return "Saturating"
    return "Watching"


def _rank(series: pd.Series, ascending: bool = False) -> pd.Series:
    return series.rank(method="min", ascending=ascending).astype(int)
