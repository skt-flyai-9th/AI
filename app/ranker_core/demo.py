from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .pipeline import RunResult, run_from_config
from .utils import stable_hash


PATTERNS: dict[str, list[int]] = {
    # Values are daily posts from 13 days ago to today.
    "demo_emerging": [0, 0, 0, 0, 0, 1, 1, 1, 2, 3, 6, 12, 22, 34],
    "demo_rising": [1, 1, 2, 2, 3, 4, 5, 7, 9, 12, 16, 21, 27, 33],
    "demo_mainstream": [25, 28, 30, 31, 34, 36, 35, 38, 40, 42, 39, 41, 43, 42],
    "demo_saturating": [42, 45, 48, 50, 52, 50, 46, 42, 36, 30, 25, 20, 15, 12],
    "demo_declining": [38, 35, 31, 28, 23, 19, 14, 10, 7, 5, 3, 2, 1, 0],
    "demo_seeded": [0, 0, 0, 1, 1, 2, 3, 4, 10, 20, 31, 42, 45, 48],
    "demo_niche": [2, 2, 3, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 8],
    "demo_global": [10, 12, 15, 18, 22, 25, 28, 30, 32, 35, 38, 40, 43, 45],
}


def create_demo_project(output_dir: str | Path, now: pd.Timestamp | None = None) -> Path:
    root = Path(output_dir).expanduser().resolve()
    data_dir = root / "data"
    result_dir = root / "output"
    data_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)

    now = now or pd.Timestamp.now(tz="UTC")
    candidates = _demo_candidates(now)
    observations = _demo_observations(candidates, now)
    candidates.to_csv(data_dir / "candidates.csv", index=False, encoding="utf-8-sig")
    observations.to_csv(data_dir / "observations.csv", index=False, encoding="utf-8-sig")

    config = {
        "timezone": "Asia/Seoul",
        "now": now.isoformat(),
        "paths": {
            "candidates_csv": "data/candidates.csv",
            "observations_csv": "data/observations.csv",
            "database": "data/challenge_ranker.sqlite3",
            "output_dir": "output",
        },
        "sources": {
            "observations": {"enabled": True},
            "youtube": {"enabled": False},
            "naver_datalab": {"enabled": False},
            "naver_blog": {"enabled": False},
            "naver_news": {"enabled": False},
            "x": {"enabled": False},
        },
        "representative_youtube": {
            "enabled": True,
            "minimum_relevance": 0.35,
        },
        "ranking": {
            "top_n": 30,
            "require_youtube_video": True,
            "confidence_floor_multiplier": 0.60,
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
    config_path = root / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config_path


def run_demo(output_dir: str | Path) -> RunResult:
    config_path = create_demo_project(output_dir)
    return run_from_config(config_path)


def _demo_candidates(now: pd.Timestamp) -> pd.DataFrame:
    specs = [
        ("demo_emerging", "데모 신생 전환", "데모 신생 전환|#데모신생", "transition", 6, 0.94, 0.95),
        ("demo_rising", "데모 상승 댄스", "데모 상승 댄스|#데모상승", "dance", 12, 0.92, 0.95),
        ("demo_mainstream", "데모 대세 포즈", "데모 대세 포즈|#데모대세", "pose", 28, 0.93, 0.96),
        ("demo_saturating", "데모 과포화 밈", "데모 과포화 밈|#데모과포화", "meme", 45, 0.90, 0.93),
        ("demo_declining", "데모 하락 챌린지", "데모 하락 챌린지|#데모하락", "dance", 60, 0.91, 0.92),
        ("demo_seeded", "데모 광고 시딩", "데모 광고 시딩|#데모시딩", "brand", 10, 0.94, 0.90),
        ("demo_niche", "데모 니치 레시피", "데모 니치 레시피|#데모니치", "food", 20, 0.96, 0.90),
        ("demo_global", "데모 해외 유행", "데모 해외 유행|#데모글로벌", "global", 15, 0.25, 0.90),
    ]
    rows = []
    for challenge_id, name, aliases, category, age, kr, entity in specs:
        rows.append(
            {
                "challenge_id": challenge_id,
                "name": name,
                "aliases": aliases,
                "category": category,
                "discovered_at": (now - pd.Timedelta(days=age)).isoformat(),
                "kr_affinity_hint": kr,
                "entity_confidence": entity,
            }
        )
    return pd.DataFrame(rows)


def _demo_observations(candidates: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    rng = random.Random(20260819)
    rows: list[dict[str, Any]] = []
    platforms = ["instagram", "youtube", "capcut", "creator_panel"]
    categories = ["dance", "beauty", "food", "comedy", "student", "fitness"]

    for candidate in candidates.itertuples(index=False):
        counts = PATTERNS[candidate.challenge_id]
        for index, count in enumerate(counts):
            days_ago = 13 - index
            for post_index in range(count):
                if candidate.challenge_id == "demo_seeded":
                    author_pool = 6
                    paid_probability = 0.82
                    platform_count = 2
                elif candidate.challenge_id == "demo_global":
                    author_pool = max(8, count // 2)
                    paid_probability = 0.04
                    platform_count = 3
                else:
                    author_pool = max(12, int(count * 0.85) + 5)
                    paid_probability = 0.06
                    platform_count = 1 + min(3, int(math.log1p(max(count, 1))))

                author_index = rng.randrange(author_pool)
                platform = platforms[(post_index + days_ago + author_index) % platform_count]
                created_at = now - pd.Timedelta(days=days_ago) - pd.Timedelta(
                    hours=rng.uniform(0.1, 23.5)
                )
                base_views = {
                    "demo_emerging": 18_000,
                    "demo_rising": 24_000,
                    "demo_mainstream": 70_000,
                    "demo_saturating": 50_000,
                    "demo_declining": 34_000,
                    "demo_seeded": 95_000,
                    "demo_niche": 12_000,
                    "demo_global": 85_000,
                }[candidate.challenge_id]
                views = int(base_views * rng.uniform(0.35, 1.9))
                followers = int(rng.choice([2_000, 8_000, 25_000, 80_000, 250_000, 1_200_000]))
                share_rate = 0.012 if candidate.challenge_id in {"demo_emerging", "demo_rising"} else 0.005
                if candidate.challenge_id == "demo_seeded":
                    share_rate = 0.002
                likes = int(views * rng.uniform(0.025, 0.09))
                comments = int(views * rng.uniform(0.0004, 0.002))
                shares = int(views * share_rate * rng.uniform(0.6, 1.4))
                is_paid = rng.random() < paid_probability
                kr_affinity = float(candidate.kr_affinity_hint)
                if candidate.challenge_id == "demo_global":
                    kr_affinity = rng.uniform(0.10, 0.35)

                content_id = (
                    "D" + stable_hash(f"{candidate.challenge_id}:{days_ago}:{post_index}", 10)
                    if platform == "youtube"
                    else f"{candidate.challenge_id}:{days_ago}:{post_index}"
                )
                rows.append(
                    {
                        "challenge_id": candidate.challenge_id,
                        "challenge_name": candidate.name,
                        "platform": platform,
                        "content_id": content_id,
                        "author_id": f"{candidate.challenge_id}:author:{author_index}",
                        "created_at": created_at.isoformat(),
                        "caption": f"{candidate.name} 참여 영상",
                        "hashtags": candidate.aliases.replace("|", " "),
                        "audio_id": f"audio:{candidate.challenge_id}",
                        "effect_id": "",
                        "template_id": f"template:{candidate.challenge_id}" if platform == "capcut" else "",
                        "views": views,
                        "likes": likes,
                        "comments": comments,
                        "shares": shares,
                        "is_paid": is_paid,
                        "kr_affinity": kr_affinity,
                        "creator_followers": followers,
                        "creator_category": categories[(author_index + post_index) % len(categories)],
                    }
                )
    return pd.DataFrame(rows)
