from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG: dict[str, Any] = {
    "timezone": "Asia/Seoul",
    "now": None,
    "paths": {
        "candidates_csv": "data/candidates.auto.csv",
        "observations_csv": "data/observations.csv",
        "database": "data/challenge_ranker.sqlite3",
        "output_dir": "output",
    },
    "auto_discovery": {
        "enabled": False,
        "gemini_api_key_env": "GEMINI_API_KEY",
        "youtube_api_key_env": "YOUTUBE_API_KEY",
        "model": "auto",
        "max_candidates": 200,
        "min_candidate_confidence": 0.12,
        "max_evidence_records": 2600,
        "ai_chunk_size": 60,
        "instagram_apify": {
            "required": False,
            "api_token_env": "APIFY_API_TOKEN",
            "search_limit_per_seed": 50,
            "max_seed_runs": 30,
            "max_expansion_runs": 30,
            "expansion_search_limit": 40,
            "timeout_seconds": 240,
            "expand_from_hashtags": True,
            "max_expansion_terms": 60,
        },
        "youtube": {
            "lookback_days": 10,
            "max_results_per_query": 25,
            "max_search_requests": 1,
            "viewcount_seed_count": 0,
            "seed_queries": [],
            "fallback_when_instagram_empty": True,
            "fallback_max_search_requests": 12,
            "fallback_viewcount_seed_count": 2,
            "fallback_seed_queries": [
                "댄스 챌린지", "유행 챌린지", "쇼츠 챌린지", "밈 챌린지",
                "포즈 챌린지", "커플 챌린지", "메이크업 챌린지", "KPOP 챌린지",
            ],
            "region_code": "KR",
            "relevance_language": "ko",
        },
        "naver": {
            "client_id_env": "NAVER_API_HUB_CLIENT_ID",
            "client_secret_env": "NAVER_API_HUB_CLIENT_SECRET",
            "display": 60,
            "sources": ["blog", "news"],
        },
    },
    "ai_adjudication": {
        "enabled": False,
        "gemini_api_key_env": "GEMINI_API_KEY",
        "model": "auto",
        "max_candidates": 200,
        "batch_size": 30,
        "weight": 0.18,
    },
    "sources": {
        "observations": {"enabled": True},
        "instagram_apify": {
            "enabled": False,
            "api_token_env": "APIFY_API_TOKEN",
            "hashtag_actor_id": "apify~instagram-hashtag-scraper",
            "results_per_challenge": 12,
            "max_terms_per_challenge": 2,
            "max_challenges": 160,
            "timeout_seconds": 240,
        },
        "youtube": {
            "enabled": False,
            "api_key_env": "YOUTUBE_API_KEY",
            "lookback_days": 365,
            "max_aliases_per_challenge": 3,
            "max_results_per_challenge": 50,
            "max_search_requests": 200,
            "max_challenges": 160,
            "search_attempts_per_challenge": 3,
            "max_duration_seconds": 900,
            "region_code": "KR",
            "relevance_language": "ko",
        },
        "naver_datalab": {
            "enabled": False,
            "client_id_env": "NAVER_API_HUB_CLIENT_ID",
            "client_secret_env": "NAVER_API_HUB_CLIENT_SECRET",
            "lookback_days": 42,
            "exclude_current_day": True,
            "recent_days": 3,
            "previous_days": 3,
            "baseline_days": 28,
        },
        "naver_blog": {
            "enabled": False,
            "client_id_env": "NAVER_API_HUB_CLIENT_ID",
            "client_secret_env": "NAVER_API_HUB_CLIENT_SECRET",
            "pages_per_challenge": 1,
            "display": 100,
            "max_aliases_per_challenge": 2,
            "query_suffix": "챌린지",
        },
        "naver_news": {
            "enabled": False,
            "client_id_env": "NAVER_API_HUB_CLIENT_ID",
            "client_secret_env": "NAVER_API_HUB_CLIENT_SECRET",
            "pages_per_challenge": 1,
            "display": 100,
            "query_suffix": "챌린지",
        },
        "x": {
            "enabled": False,
            "bearer_token_env": "X_BEARER_TOKEN",
            "lookback_days": 7,
            "language": "ko",
            "exclude_retweets": True,
            "max_aliases_per_challenge": 5,
        },
    },
    "representative_youtube": {
        "enabled": True,
        "gemini_api_key_env": "GEMINI_API_KEY",
        "model": "auto",
        "ai_participation_check": True,
        "max_ai_videos_per_challenge": 10,
        "max_ai_challenges_per_run": 100,
        "ai_batch_challenges": 12,
        "minimum_relevance": 0.32,
        "fallback_enabled": True,
        "fallback_minimum_relevance": 0.14,
        "recency_half_life_days": 35,
        "paid_penalty": 0.08,
        "representative_weights": {
            "relevance": 0.25,
            "participation": 0.10,
            "popularity": 0.45,
            "recency": 0.05,
            "engagement": 0.05,
            "kr_affinity": 0.10,
        },
        "guide_weights": {
            "relevance": 0.20,
            "guideability": 0.45,
            "participation": 0.10,
            "popularity": 0.05,
            "recency": 0.05,
            "engagement": 0.05,
            "kr_affinity": 0.10,
        },
    },
    "ranking": {
        "top_n": 100,
        "require_youtube_video": False,
        "exclude_ai_rejected": True,
        "backfill_to_top_n": True,
        "backfill_min_entity_confidence": 0.15,
        "confidence_floor_multiplier": 0.55,
        "emerging_weights": {
            "participation_acceleration": 0.25,
            "kr_creator_growth": 0.20,
            "creator_diversity": 0.15,
            "cross_platform": 0.15,
            "search_lift": 0.10,
            "freshness": 0.10,
            "participation_conversion": 0.05,
        },
        "mainstream_weights": {
            "unique_creators": 0.25,
            "reach": 0.20,
            "persistence": 0.15,
            "cross_platform": 0.15,
            "spillover": 0.10,
            "creator_diversity": 0.10,
            "organic_breadth": 0.05,
        },
        "seeded_penalty_max": 15.0,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path) -> tuple[dict[str, Any], Path]:
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"설정 파일을 찾을 수 없습니다: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("설정 파일 최상위는 YAML 객체여야 합니다.")

    config = _deep_merge(DEFAULT_CONFIG, raw)
    base_dir = config_path.parent

    for key in ("candidates_csv", "observations_csv", "database", "output_dir"):
        value = Path(str(config["paths"][key])).expanduser()
        if not value.is_absolute():
            value = (base_dir / value).resolve()
        config["paths"][key] = str(value)

    _validate_weights(config["representative_youtube"]["representative_weights"], "representative_youtube.representative_weights")
    _validate_weights(config["representative_youtube"]["guide_weights"], "representative_youtube.guide_weights")
    _validate_weights(config["ranking"]["emerging_weights"], "emerging_weights")
    _validate_weights(config["ranking"]["mainstream_weights"], "mainstream_weights")

    return config, config_path


def _validate_weights(weights: dict[str, Any], name: str) -> None:
    total = 0.0
    for key, value in weights.items():
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}.{key}는 숫자여야 합니다.") from exc
        if numeric < 0:
            raise ValueError(f"{name}.{key}는 0 이상이어야 합니다.")
        total += numeric
    if total <= 0:
        raise ValueError(f"{name} 가중치 합은 0보다 커야 합니다.")
