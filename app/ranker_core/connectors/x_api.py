from __future__ import annotations

import os
from typing import Any

import pandas as pd
import requests

from ..utils import request_json, smoothed_growth
from .base import ConnectorResult


class XCountsConnector:
    source = "x"
    url = "https://api.x.com/2/tweets/counts/recent"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "challenge-ranker/0.1"})
        self.request_count = 0

    def collect(self, candidates: pd.DataFrame, now: pd.Timestamp) -> ConnectorResult:
        token_name = str(self.config.get("bearer_token_env", "X_BEARER_TOKEN"))
        token = os.getenv(token_name, "").strip()
        if not token:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame({"challenge_id": candidates["challenge_id"]}),
                status={
                    "enabled": True,
                    "success": False,
                    "skipped": True,
                    "reason": f"환경변수 {token_name}가 없습니다.",
                },
            )

        headers = {"Authorization": f"Bearer {token}"}
        try:
            records = [self._collect_one(candidate, now, headers) for candidate in candidates.itertuples(index=False)]
            metrics = pd.DataFrame(records)
            return ConnectorResult(
                source=self.source,
                metrics=metrics,
                status={
                    "enabled": True,
                    "success": True,
                    "requests": self.request_count,
                    "rows": int((metrics["x_evidence"] > 0).sum()),
                },
            )
        except Exception as exc:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame({"challenge_id": candidates["challenge_id"]}),
                status={
                    "enabled": True,
                    "success": False,
                    "skipped": False,
                    "error": str(exc),
                    "requests": self.request_count,
                },
            )

    def _collect_one(
        self, candidate: Any, now: pd.Timestamp, headers: dict[str, str]
    ) -> dict[str, Any]:
        lookback_days = min(7, max(2, int(self.config.get("lookback_days", 7))))
        max_aliases = max(1, int(self.config.get("max_aliases_per_challenge", 5)))
        query = build_x_query(
            list(candidate.alias_list)[:max_aliases],
            language=str(self.config.get("language", "ko")).strip(),
            exclude_retweets=bool(self.config.get("exclude_retweets", True)),
        )
        start_time = now - pd.Timedelta(days=lookback_days) + pd.Timedelta(minutes=2)
        end_time = now - pd.Timedelta(minutes=2)
        payload = request_json(
            self.session,
            "GET",
            self.url,
            headers=headers,
            params={
                "query": query,
                "start_time": start_time.isoformat().replace("+00:00", "Z"),
                "end_time": end_time.isoformat().replace("+00:00", "Z"),
                "granularity": "hour",
            },
        )
        self.request_count += 1

        bins: list[tuple[pd.Timestamp, int]] = []
        for item in payload.get("data", []):
            start = pd.to_datetime(item.get("start"), utc=True, errors="coerce")
            if pd.isna(start):
                continue
            bins.append((start, int(item.get("post_count", 0))))

        def count_between(start: pd.Timestamp, end: pd.Timestamp) -> int:
            return sum(count for ts, count in bins if start <= ts < end)

        posts_24h = count_between(end_time - pd.Timedelta(hours=24), end_time)
        posts_prev24h = count_between(
            end_time - pd.Timedelta(hours=48), end_time - pd.Timedelta(hours=24)
        )
        posts_72h = count_between(end_time - pd.Timedelta(hours=72), end_time)
        posts_prev72h = count_between(
            end_time - pd.Timedelta(hours=144), end_time - pd.Timedelta(hours=72)
        )
        posts_7d = count_between(end_time - pd.Timedelta(days=7), end_time)

        return {
            "challenge_id": candidate.challenge_id,
            "x_posts_24h": float(posts_24h),
            "x_posts_prev24h": float(posts_prev24h),
            "x_posts_72h": float(posts_72h),
            "x_posts_prev72h": float(posts_prev72h),
            "x_posts_7d": float(posts_7d),
            "x_post_growth_24h": smoothed_growth(posts_24h, posts_prev24h, alpha=2.0),
            "x_post_growth_72h": smoothed_growth(posts_72h, posts_prev72h, alpha=3.0),
            "x_evidence": 1.0 if posts_7d > 0 else 0.0,
        }


def build_x_query(aliases: list[str], language: str = "ko", exclude_retweets: bool = True) -> str:
    terms: list[str] = []
    for alias in aliases:
        text = str(alias).strip()
        if not text:
            continue
        if text.startswith("#") and " " not in text:
            terms.append(text)
        else:
            escaped = text.replace('"', '\\"')
            terms.append(f'"{escaped}"')
    if not terms:
        raise ValueError("X 검색에 사용할 alias가 없습니다.")
    query = f"({' OR '.join(terms)})"
    if language:
        query += f" lang:{language}"
    if exclude_retweets:
        query += " -is:retweet"
    if len(query) > 4096:
        raise ValueError("X 검색 쿼리가 4096자를 초과합니다.")
    return query
