from __future__ import annotations

import os
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from ..utils import chunks, request_json, smoothed_growth, strip_html
from .base import ConnectorResult


NAVER_API_HUB_BASE_URL = "https://naverapihub.apigw.ntruss.com"
NAVER_API_HUB_CLIENT_ID_HEADER = "X-NCP-APIGW-API-KEY-ID"
NAVER_API_HUB_CLIENT_SECRET_HEADER = "X-NCP-APIGW-API-KEY"


class NaverDatalabConnector:
    """Collect NAVER Search Trend metrics through NAVER Cloud NAVER API HUB."""

    source = "naver_datalab"
    url = f"{NAVER_API_HUB_BASE_URL}/search-trend/v1/search"

    def __init__(self, config: dict[str, Any], timezone_name: str):
        self.config = config
        self.timezone_name = timezone_name
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "challenge-ranker/0.4"})
        self.request_count = 0

    def collect(self, candidates: pd.DataFrame, now: pd.Timestamp) -> ConnectorResult:
        credentials = _naver_api_hub_credentials(self.config)
        if credentials is None:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame({"challenge_id": candidates["challenge_id"]}),
                status={
                    "enabled": True,
                    "success": False,
                    "skipped": True,
                    "reason": _missing_credentials_reason(self.config),
                },
            )
        client_id, client_secret = credentials
        headers = _naver_api_hub_headers(client_id, client_secret, json_body=True)
        try:
            metrics = self._collect(candidates, now, headers)
            return ConnectorResult(
                source=self.source,
                metrics=metrics,
                status={
                    "enabled": True,
                    "success": True,
                    "requests": self.request_count,
                    "rows": int((metrics["naver_search_evidence"] > 0).sum()),
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

    def _collect(
        self, candidates: pd.DataFrame, now: pd.Timestamp, headers: dict[str, str]
    ) -> pd.DataFrame:
        lookback_days = max(14, int(self.config.get("lookback_days", 42)))
        recent_days = max(1, int(self.config.get("recent_days", 3)))
        previous_days = max(1, int(self.config.get("previous_days", 3)))
        baseline_days = max(7, int(self.config.get("baseline_days", 28)))

        local_now = now.tz_convert(ZoneInfo(self.timezone_name))
        end_date = local_now.date()
        if bool(self.config.get("exclude_current_day", True)):
            end_date = end_date - pd.Timedelta(days=1)
        start_date = end_date - pd.Timedelta(days=lookback_days - 1)
        all_dates = pd.date_range(start=start_date, end=end_date, freq="D")

        records: list[dict[str, Any]] = []
        candidate_rows = list(candidates.itertuples(index=False))
        for batch_index, batch in enumerate(chunks(candidate_rows, 5)):
            groups = []
            group_to_id: dict[str, str] = {}
            for item_index, candidate in enumerate(batch):
                group_name = f"g{batch_index}_{item_index}"
                aliases = [str(alias).lstrip("#") for alias in candidate.alias_list[:20] if str(alias).strip()]
                if not aliases:
                    aliases = [candidate.name]
                groups.append({"groupName": group_name, "keywords": aliases[:5]})
                group_to_id[group_name] = candidate.challenge_id

            payload = request_json(
                self.session,
                "POST",
                self.url,
                headers=headers,
                json={
                    "startDate": str(start_date),
                    "endDate": str(end_date),
                    "timeUnit": "date",
                    "keywordGroups": groups,
                },
            )
            self.request_count += 1
            by_id: dict[str, dict[str, Any]] = {}
            for result in payload.get("results", []):
                challenge_id = group_to_id.get(str(result.get("title", "")))
                if not challenge_id:
                    continue
                series = pd.Series(0.0, index=all_dates)
                for point in result.get("data", []):
                    period = pd.to_datetime(point.get("period"), errors="coerce")
                    if pd.isna(period):
                        continue
                    series.loc[period.normalize()] = float(point.get("ratio", 0.0))
                by_id[challenge_id] = _search_metrics(
                    series, recent_days, previous_days, baseline_days
                )

            for candidate in batch:
                metrics = by_id.get(candidate.challenge_id, _empty_search_metrics())
                records.append({"challenge_id": candidate.challenge_id, **metrics})

        return pd.DataFrame(records)


class NaverBlogConnector:
    """Measure Korean user-generated discussion via NAVER API HUB Blog Search.

    Naver Blog Search is a validation signal, not a source-of-truth count of every
    blog post. The API returns a ranked/search-index sample, so the ranker uses
    recent-window counts and growth rather than treating ``total`` as an absolute
    participation number.
    """

    source = "naver_blog"
    url = f"{NAVER_API_HUB_BASE_URL}/search/v1/blog"

    def __init__(self, config: dict[str, Any], timezone_name: str = "Asia/Seoul"):
        self.config = config
        self.timezone_name = timezone_name
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "challenge-ranker/0.4"})
        self.request_count = 0

    def collect(self, candidates: pd.DataFrame, now: pd.Timestamp) -> ConnectorResult:
        credentials = _naver_api_hub_credentials(self.config)
        if credentials is None:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame({"challenge_id": candidates["challenge_id"]}),
                status={
                    "enabled": True,
                    "success": False,
                    "skipped": True,
                    "reason": _missing_credentials_reason(self.config),
                },
            )
        client_id, client_secret = credentials
        headers = _naver_api_hub_headers(client_id, client_secret)
        try:
            records = [
                self._collect_one(candidate, now, headers)
                for candidate in candidates.itertuples(index=False)
            ]
            metrics = pd.DataFrame(records)
            return ConnectorResult(
                source=self.source,
                metrics=metrics,
                status={
                    "enabled": True,
                    "success": True,
                    "requests": self.request_count,
                    "rows": int((metrics["naver_blog_evidence"] > 0).sum()),
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
        display = min(100, max(1, int(self.config.get("display", 100))))
        pages = min(10, max(1, int(self.config.get("pages_per_challenge", 1))))
        max_aliases = min(5, max(1, int(self.config.get("max_aliases_per_challenge", 2))))
        suffix = str(self.config.get("query_suffix", "챌린지")).strip()

        aliases = [str(value).lstrip("#").strip() for value in candidate.alias_list if str(value).strip()]
        if not aliases:
            aliases = [str(candidate.name).strip()]

        items: dict[str, dict[str, Any]] = {}
        for alias in aliases[:max_aliases]:
            query = alias if suffix and suffix in alias else " ".join(
                part for part in (alias, suffix) if part
            )
            for page in range(pages):
                start = 1 + page * display
                if start > 1000:
                    break
                payload = request_json(
                    self.session,
                    "GET",
                    self.url,
                    headers=headers,
                    params={
                        "query": query,
                        "display": display,
                        "start": start,
                        "sort": "date",
                        "format": "json",
                    },
                )
                self.request_count += 1
                returned = payload.get("items", [])
                for item in returned:
                    link = str(item.get("link") or "").strip()
                    key = link or f"{strip_html(str(item.get('title', '')))}|{item.get('postdate')}"
                    items[key] = item
                if len(returned) < display:
                    break

        local_tz = ZoneInfo(self.timezone_name)
        dated_items: list[tuple[pd.Timestamp, str]] = []
        for item in items.values():
            postdate = str(item.get("postdate", "")).strip()
            try:
                ts = pd.to_datetime(postdate, format="%Y%m%d", errors="raise")
                ts = pd.Timestamp(ts).tz_localize(local_tz).tz_convert("UTC")
            except (TypeError, ValueError, OverflowError):
                continue
            author = str(item.get("bloggerlink") or item.get("bloggername") or "").strip()
            dated_items.append((ts, author))

        def rows_between(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, str]]:
            return [(ts, author) for ts, author in dated_items if start <= ts < end]

        current_1d_rows = rows_between(now - pd.Timedelta(days=1), now)
        current_7d_rows = rows_between(now - pd.Timedelta(days=7), now)
        previous_7d_rows = rows_between(now - pd.Timedelta(days=14), now - pd.Timedelta(days=7))
        current_30d_rows = rows_between(now - pd.Timedelta(days=30), now)
        authors_7d = len({author for _, author in current_7d_rows if author})

        current_1d = len(current_1d_rows)
        current_7d = len(current_7d_rows)
        previous_7d = len(previous_7d_rows)
        current_30d = len(current_30d_rows)
        return {
            "challenge_id": candidate.challenge_id,
            "naver_blog_1d": float(current_1d),
            "naver_blog_7d": float(current_7d),
            "naver_blog_prev7d": float(previous_7d),
            "naver_blog_30d": float(current_30d),
            "naver_blog_authors_7d": float(authors_7d),
            "naver_blog_growth_7d": smoothed_growth(current_7d, previous_7d, alpha=1.0),
            "naver_blog_evidence": 1.0 if current_30d > 0 else 0.0,
        }


class NaverNewsConnector:
    """Collect NAVER News Search results through NAVER API HUB."""

    source = "naver_news"
    url = f"{NAVER_API_HUB_BASE_URL}/search/v1/news"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "challenge-ranker/0.4"})
        self.request_count = 0

    def collect(self, candidates: pd.DataFrame, now: pd.Timestamp) -> ConnectorResult:
        credentials = _naver_api_hub_credentials(self.config)
        if credentials is None:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame({"challenge_id": candidates["challenge_id"]}),
                status={
                    "enabled": True,
                    "success": False,
                    "skipped": True,
                    "reason": _missing_credentials_reason(self.config),
                },
            )
        client_id, client_secret = credentials
        headers = _naver_api_hub_headers(client_id, client_secret)
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
                    "rows": int((metrics["naver_news_evidence"] > 0).sum()),
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
        display = min(100, max(1, int(self.config.get("display", 100))))
        pages = min(10, max(1, int(self.config.get("pages_per_challenge", 1))))
        suffix = str(self.config.get("query_suffix", "챌린지")).strip()
        name = str(candidate.name).strip()
        query = name if suffix and suffix in name else " ".join(part for part in (name, suffix) if part)

        items: dict[str, dict[str, Any]] = {}
        for page in range(pages):
            start = 1 + page * display
            if start > 1000:
                break
            payload = request_json(
                self.session,
                "GET",
                self.url,
                headers=headers,
                params={
                    "query": query,
                    "display": display,
                    "start": start,
                    "sort": "date",
                    "format": "json",
                },
            )
            self.request_count += 1
            returned = payload.get("items", [])
            for item in returned:
                url = str(item.get("originallink") or item.get("link") or "").strip()
                key = url or f"{item.get('title')}|{item.get('pubDate')}"
                items[key] = item
            if len(returned) < display:
                break

        dates: list[pd.Timestamp] = []
        for item in items.values():
            try:
                parsed = parsedate_to_datetime(str(item.get("pubDate", "")))
                ts = pd.Timestamp(parsed)
                ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
                dates.append(ts)
            except (TypeError, ValueError, OverflowError):
                continue

        def count_between(start: pd.Timestamp, end: pd.Timestamp) -> int:
            return sum(1 for ts in dates if start <= ts < end)

        current_1d = count_between(now - pd.Timedelta(days=1), now)
        current_7d = count_between(now - pd.Timedelta(days=7), now)
        previous_7d = count_between(now - pd.Timedelta(days=14), now - pd.Timedelta(days=7))
        current_30d = count_between(now - pd.Timedelta(days=30), now)

        return {
            "challenge_id": candidate.challenge_id,
            "naver_news_1d": float(current_1d),
            "naver_news_7d": float(current_7d),
            "naver_news_prev7d": float(previous_7d),
            "naver_news_30d": float(current_30d),
            "naver_news_growth_7d": smoothed_growth(current_7d, previous_7d, alpha=1.0),
            "naver_news_evidence": 1.0 if current_30d > 0 else 0.0,
        }


def _search_metrics(
    series: pd.Series, recent_days: int, previous_days: int, baseline_days: int
) -> dict[str, float]:
    values = series.astype(float)
    recent = values.iloc[-recent_days:] if len(values) >= recent_days else values
    previous_end = len(values) - recent_days
    previous_start = max(0, previous_end - previous_days)
    previous = values.iloc[previous_start:previous_end]
    baseline_end = previous_start
    baseline_start = max(0, baseline_end - baseline_days)
    baseline = values.iloc[baseline_start:baseline_end]

    recent_mean = float(recent.mean()) if len(recent) else 0.0
    previous_mean = float(previous.mean()) if len(previous) else 0.0
    baseline_mean = float(baseline.mean()) if len(baseline) else 0.0
    alpha = 0.5
    return {
        "naver_search_recent_mean": recent_mean,
        "naver_search_previous_mean": previous_mean,
        "naver_search_baseline_mean": baseline_mean,
        "naver_search_lift_3d": (recent_mean + alpha) / (baseline_mean + alpha) - 1.0,
        "naver_search_acceleration": (recent_mean + alpha) / (previous_mean + alpha) - 1.0,
        "naver_search_evidence": 1.0 if float(values.max()) > 0 else 0.0,
        "naver_search_sample_days": float((values > 0).sum()),
    }


def _empty_search_metrics() -> dict[str, float]:
    return {
        "naver_search_recent_mean": 0.0,
        "naver_search_previous_mean": 0.0,
        "naver_search_baseline_mean": 0.0,
        "naver_search_lift_3d": 0.0,
        "naver_search_acceleration": 0.0,
        "naver_search_evidence": 0.0,
        "naver_search_sample_days": 0.0,
    }


def _naver_api_hub_credentials(config: dict[str, Any]) -> tuple[str, str] | None:
    """Load NAVER API HUB application credentials from configured env vars.

    NAVER Developers Center credentials are intentionally not used here because
    they are not compatible with NAVER API HUB credentials.
    """

    id_name = str(config.get("client_id_env", "NAVER_API_HUB_CLIENT_ID"))
    secret_name = str(
        config.get("client_secret_env", "NAVER_API_HUB_CLIENT_SECRET")
    )
    client_id = os.getenv(id_name, "").strip()
    client_secret = os.getenv(secret_name, "").strip()
    if not client_id or not client_secret:
        return None
    return client_id, client_secret


def _naver_api_hub_headers(
    client_id: str, client_secret: str, *, json_body: bool = False
) -> dict[str, str]:
    headers = {
        NAVER_API_HUB_CLIENT_ID_HEADER: client_id,
        NAVER_API_HUB_CLIENT_SECRET_HEADER: client_secret,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _missing_credentials_reason(config: dict[str, Any]) -> str:
    id_name = str(config.get("client_id_env", "NAVER_API_HUB_CLIENT_ID"))
    secret_name = str(
        config.get("client_secret_env", "NAVER_API_HUB_CLIENT_SECRET")
    )
    return (
        f"NAVER API HUB 인증 환경변수가 없습니다: {id_name}, {secret_name}. "
        "NAVER Cloud Platform의 NAVER API HUB에서 새 인증 정보를 발급받아야 합니다."
    )
