from __future__ import annotations

import os
import re
from collections import defaultdict
from typing import Any

import pandas as pd
import requests

from ..aggregation import aggregate_content_rows
from ..utils import chunks, clip01, korean_ratio, parse_bool, request_json, safe_int
from .base import ConnectorResult


class YouTubeConnector:
    source = "youtube"
    search_url = "https://www.googleapis.com/youtube/v3/search"
    videos_url = "https://www.googleapis.com/youtube/v3/videos"
    channels_url = "https://www.googleapis.com/youtube/v3/channels"

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "challenge-ranker/2.0"})
        self.request_count = 0
        self.search_request_count = 0

    def collect(self, candidates: pd.DataFrame, now: pd.Timestamp) -> ConnectorResult:
        key_name = str(self.config.get("api_key_env", "YOUTUBE_API_KEY"))
        api_key = os.getenv(key_name, "").strip()
        if not api_key:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame({"challenge_id": candidates["challenge_id"]}),
                raw_rows=_empty_rows(),
                status={
                    "enabled": True,
                    "success": False,
                    "skipped": True,
                    "reason": f"환경변수 {key_name}가 없습니다.",
                },
            )

        try:
            rows = self._collect_rows(candidates, now, api_key)
            metrics = aggregate_content_rows(candidates, rows, now, prefix="youtube")
            return ConnectorResult(
                source=self.source,
                metrics=metrics,
                raw_rows=rows,
                status={
                    "enabled": True,
                    "success": True,
                    "requests": self.request_count,
                    "search_requests": self.search_request_count,
                    "searched_challenges": min(
                        len(candidates), int(self.config.get("max_challenges", 100))
                    ),
                    "rows": int(len(rows)),
                },
            )
        except Exception as exc:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame({"challenge_id": candidates["challenge_id"]}),
                raw_rows=_empty_rows(),
                status={
                    "enabled": True,
                    "success": False,
                    "skipped": False,
                    "error": str(exc),
                    "requests": self.request_count,
                    "search_requests": self.search_request_count,
                },
            )

    def _collect_rows(
        self, candidates: pd.DataFrame, now: pd.Timestamp, api_key: str
    ) -> pd.DataFrame:
        # One search.list per challenge. This intentionally uses most of the daily
        # search bucket because the product needs two high-quality links for each
        # of the Top 100 challenges. Both app-card and guide links are selected
        # from the same up-to-50-video result set, so we do NOT spend two searches
        # per challenge.
        lookback_days = max(30, int(self.config.get("lookback_days", 180)))
        max_aliases = max(1, int(self.config.get("max_aliases_per_challenge", 3)))
        max_results = min(50, max(10, int(self.config.get("max_results_per_challenge", 50))))
        budget = min(100, max(1, int(self.config.get("max_search_requests", 100))))
        max_challenges = min(budget, max(1, int(self.config.get("max_challenges", 100))))
        published_after = (now - pd.Timedelta(days=lookback_days)).isoformat().replace(
            "+00:00", "Z"
        )

        video_matches: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        searched_ids: list[str] = []

        for candidate in candidates.head(max_challenges).itertuples(index=False):
            if self.search_request_count >= budget:
                break
            aliases = [
                str(x).strip()
                for x in list(getattr(candidate, "alias_list", []) or [])[:max_aliases]
                if str(x).strip()
            ]
            name = str(candidate.name).strip()
            if name and name not in aliases:
                aliases.insert(0, name)
            aliases = list(dict.fromkeys(aliases))[:max_aliases]
            if not aliases:
                continue

            max_attempts = max(1, int(self.config.get("search_attempts_per_challenge", 3)))
            for query in _build_query_attempts(aliases)[:max_attempts]:
                if self.search_request_count >= budget:
                    break
                params = {
                    "key": api_key,
                    "part": "snippet",
                    "type": "video",
                    "q": query,
                    "order": "relevance",
                    "publishedAfter": published_after,
                    "maxResults": max_results,
                    # Do not restrict to <4 min. Follow-along choreography/practice
                    # videos are often longer than Shorts.
                    "regionCode": self.config.get("region_code", "KR"),
                    "relevanceLanguage": self.config.get("relevance_language", "ko"),
                    "safeSearch": "moderate",
                }
                payload = request_json(self.session, "GET", self.search_url, params=params)
                self.request_count += 1
                self.search_request_count += 1
                searched_ids.append(str(candidate.challenge_id))
                found = False
                for item in payload.get("items", []):
                    video_id = str(item.get("id", {}).get("videoId", "")).strip()
                    if not video_id:
                        continue
                    found = True
                    # This call is challenge-specific. Downstream semantic scoring
                    # checks title/description/tags before choosing either link.
                    for alias in aliases:
                        video_matches[video_id][str(candidate.challenge_id)].add(alias)
                if found:
                    break

        if not video_matches:
            return _empty_rows()

        details: dict[str, dict[str, Any]] = {}
        all_video_ids = list(video_matches)
        for batch in chunks(all_video_ids, 50):
            params = {
                "key": api_key,
                "part": "snippet,statistics,contentDetails,paidProductPlacementDetails",
                "id": ",".join(batch),
            }
            payload = request_json(self.session, "GET", self.videos_url, params=params)
            self.request_count += 1
            for item in payload.get("items", []):
                details[str(item.get("id"))] = item

        channel_ids = sorted(
            {
                str(item.get("snippet", {}).get("channelId", ""))
                for item in details.values()
                if item.get("snippet", {}).get("channelId")
            }
        )
        channel_stats: dict[str, int] = {}
        for batch in chunks(channel_ids, 50):
            params = {
                "key": api_key,
                "part": "snippet,statistics",
                "id": ",".join(batch),
            }
            payload = request_json(self.session, "GET", self.channels_url, params=params)
            self.request_count += 1
            for item in payload.get("items", []):
                channel_stats[str(item.get("id"))] = safe_int(
                    item.get("statistics", {}).get("subscriberCount", 0)
                )

        max_duration = int(self.config.get("max_duration_seconds", 900))
        rows: list[dict[str, Any]] = []
        for video_id, challenge_aliases in video_matches.items():
            item = details.get(video_id)
            if not item:
                continue
            duration_seconds = _duration_seconds(item.get("contentDetails", {}).get("duration"))
            if duration_seconds <= 0 or duration_seconds > max_duration:
                continue

            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            channel_id = str(snippet.get("channelId", ""))
            channel_title = str(snippet.get("channelTitle", ""))
            title = str(snippet.get("title", ""))
            description = str(snippet.get("description", ""))
            tags = snippet.get("tags", []) or []
            text = " ".join([title, description, " ".join(map(str, tags))])
            text_kr_ratio = korean_ratio(text)
            kr_affinity = clip01(0.15 + 0.85 * text_kr_ratio)
            paid = parse_bool(
                item.get("paidProductPlacementDetails", {}).get(
                    "hasPaidProductPlacement", False
                )
            )

            for challenge_id, matched_aliases in challenge_aliases.items():
                matched_alias = sorted(matched_aliases, key=lambda value: (-len(value), value))[0]
                rows.append(
                    {
                        "challenge_id": challenge_id,
                        "challenge_name": "",
                        "platform": "youtube",
                        "content_id": video_id,
                        "author_id": channel_id or f"youtube:{video_id}",
                        "created_at": snippet.get("publishedAt"),
                        "title": title,
                        "channel_title": channel_title,
                        "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
                        "matched_alias": matched_alias,
                        "duration_seconds": duration_seconds,
                        "source_origin": "youtube_api",
                        "caption": text,
                        "hashtags": "|".join(str(tag) for tag in tags),
                        "audio_id": "",
                        "effect_id": "",
                        "template_id": "",
                        "views": safe_int(statistics.get("viewCount", 0)),
                        "likes": safe_int(statistics.get("likeCount", 0)),
                        "comments": safe_int(statistics.get("commentCount", 0)),
                        # Public videos.list does not provide general shareCount.
                        "shares": 0,
                        "is_paid": paid,
                        "kr_affinity": kr_affinity,
                        "creator_followers": channel_stats.get(channel_id, 0),
                        "creator_category": "youtube",
                    }
                )

        frame = pd.DataFrame(rows)
        if frame.empty:
            return _empty_rows()
        frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True, errors="coerce")
        return frame[frame["created_at"].notna()].copy()


def _build_query_terms(aliases: list[str]) -> list[str]:
    """Build one broad query that can surface both famous and follow-along videos."""
    terms: list[str] = []
    for alias in aliases:
        value = str(alias).strip()
        if not value:
            continue
        terms.append(value)

    # Use the canonical/first alias to widen guide coverage without a second
    # search.list request. YouTube OR syntax keeps it to one request.
    base = aliases[0]
    for suffix in (
        "안무", "안무영상", "tutorial", "dance practice", "choreography",
        "거울모드", "mirrored", "slow", "연습영상",
    ):
        terms.append(f"{base} {suffix}")

    # Keep the query reasonably short while preserving the explicit guide terms.
    return list(dict.fromkeys(terms))[:12]


def _build_query_attempts(aliases: list[str]) -> list[str]:
    """Return progressively broader queries for candidates with no video hit."""

    primary = "|".join(_build_query_terms(aliases))
    base = aliases[0].strip()
    attempts = [
        primary,
        f'"{base}" 챌린지 shorts',
        f'"{base}" challenge tutorial',
    ]
    return [query for query in dict.fromkeys(attempts) if query.strip()]


def _duration_seconds(value: Any) -> int:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        str(value or ""),
    )
    if not match:
        return 0
    parts = {key: int(number or 0) for key, number in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def _empty_rows() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "challenge_id", "challenge_name", "platform", "content_id", "author_id",
            "created_at", "title", "channel_title", "youtube_url", "matched_alias",
            "duration_seconds", "source_origin", "caption", "hashtags", "audio_id",
            "effect_id", "template_id", "views", "likes", "comments", "shares",
            "is_paid", "kr_affinity", "creator_followers", "creator_category",
        ]
    )
