from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd
import requests

from ..aggregation import aggregate_content_rows
from ..utils import clip01, korean_ratio, normalize_text, parse_bool, safe_float, safe_int
from .base import ConnectorResult

APIFY_BASE_URL = "https://api.apify.com/v2"
SEARCH_ACTOR = "apify~instagram-search-scraper"
HASHTAG_ACTOR = "apify~instagram-hashtag-scraper"

_GENERIC_HASHTAGS = {
    "reels", "reel", "instagram", "instareels", "viral", "fyp", "foryou", "foryoupage",
    "explore", "explorepage", "추천", "릴스", "인스타", "인스타그램", "일상", "데일리",
}


def _apify_error_message(response: requests.Response, actor_id: str) -> str:
    try:
        payload = response.json()
        body = json.dumps(payload, ensure_ascii=False)[:1600]
    except Exception:
        body = (response.text or "").replace("\n", " ")[:1600]
    return f"Apify Actor HTTP {response.status_code} ({actor_id}): {body or 'empty response'}"


def run_actor_items(
    *, token: str, actor_id: str, run_input: dict[str, Any], timeout_seconds: int = 240,
    max_attempts: int = 3,
) -> list[dict[str, Any]]:
    """Run an Apify Actor and return dataset items.

    Transient Instagram/Apify failures (408/429/5xx) are retried. Permanent
    failures include the actual HTTP status/body so users can diagnose token,
    credit, Actor or input problems instead of seeing a generic message.
    """
    url = f"{APIFY_BASE_URL}/acts/{actor_id}/run-sync-get-dataset-items"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "challenge-ranker-instagram/2.1",
    }
    attempts = max(1, int(max_attempts))
    transient = {408, 425, 429, 500, 502, 503, 504}
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.post(
                url,
                headers=headers,
                params={"timeout": min(300, max(30, timeout_seconds))},
                json=run_input,
                timeout=max(60, timeout_seconds + 20),
            )
        except requests.RequestException as exc:
            last_error = f"Apify 네트워크 오류 ({actor_id}): {exc}"
            if attempt + 1 < attempts:
                time.sleep(min(8, 2 ** attempt * 2))
                continue
            raise RuntimeError(last_error) from exc

        if response.status_code >= 400:
            last_error = _apify_error_message(response, actor_id)
            if response.status_code in transient and attempt + 1 < attempts:
                time.sleep(min(8, 2 ** attempt * 2))
                continue
            raise RuntimeError(last_error)

        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError(
                f"Apify Actor JSON 파싱 실패 ({actor_id}): {(response.text or '')[:800]}"
            ) from exc
        if not isinstance(payload, list):
            body = json.dumps(payload, ensure_ascii=False)[:1600] if isinstance(payload, (dict, list)) else str(payload)[:1600]
            raise RuntimeError(
                f"Apify Actor 응답이 dataset item 배열이 아닙니다 ({actor_id}): {body}"
            )
        return [item for item in payload if isinstance(item, dict)]

    raise RuntimeError(last_error or f"Apify Actor 호출 실패: {actor_id}")


def collect_popular_reels(
    *, token: str, seeds: list[str], search_limit: int = 24, timeout_seconds: int = 240
) -> list[dict[str, Any]]:
    terms = [clean_search_term(x) for x in seeds]
    terms = [x for x in terms if x]
    if not terms:
        return []
    return run_actor_items(
        token=token,
        actor_id=SEARCH_ACTOR,
        run_input={
            "search": ",".join(dict.fromkeys(terms)),
            "searchType": "popular",
            "searchLimit": min(250, max(1, int(search_limit))),
        },
        timeout_seconds=timeout_seconds,
    )


def collect_popular_reels_resilient(
    *, token: str, seeds: list[str], search_limit: int = 24, timeout_seconds: int = 240,
    max_seed_runs: int = 10, min_successful_seeds: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect popular reels seed-by-seed so one bad keyword cannot kill discovery.

    Instagram does not expose a popular-reels feed for every keyword, and Actor
    runs can be transiently blocked. Failed/empty seeds are recorded and skipped.
    """
    terms = [clean_search_term(x) for x in seeds]
    terms = list(dict.fromkeys(x for x in terms if x))[: max(1, int(max_seed_runs))]
    all_items: list[dict[str, Any]] = []
    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for term in terms:
        try:
            rows = run_actor_items(
                token=token,
                actor_id=SEARCH_ACTOR,
                run_input={
                    "search": term,
                    "searchType": "popular",
                    "searchLimit": min(250, max(1, int(search_limit))),
                },
                timeout_seconds=timeout_seconds,
            )
            if rows:
                succeeded.append({"term": term, "rows": len(rows)})
                for row in rows:
                    row = dict(row)
                    row.setdefault("_challenge_ranker_seed", term)
                    all_items.append(row)
            else:
                failed.append({"term": term, "reason": "0 results / popular feed unavailable"})
        except Exception as exc:
            failed.append({"term": term, "reason": str(exc)[:1200]})

    # Dedupe the same reel returned for multiple seeds.
    dedup: dict[str, dict[str, Any]] = {}
    for item in all_items:
        key = str(item.get("id") or item.get("shortCode") or item.get("url") or hash(str(item)))
        dedup[key] = item
    items = list(dedup.values())
    report = {
        "attempted_terms": len(terms),
        "successful_terms": len(succeeded),
        "succeeded": succeeded,
        "failed": failed,
        "rows": len(items),
        "minimum_success_met": len(succeeded) >= max(1, int(min_successful_seeds)),
    }
    return items, report


def derive_expansion_terms(items: list[dict[str, Any]], max_terms: int = 8) -> list[str]:
    """Expand discovery from observed hashtags AND repeated music names.

    Popular-reels discovery is seed based. Reusing newly observed challenge-like
    hashtags and repeated songs lets the crawler move toward Instagram-native
    trends without any human candidate list.
    """
    counts: Counter[str] = Counter()
    music_counts: Counter[str] = Counter()
    for item in items:
        caption = str(item.get("caption") or "")
        for raw in item.get("hashtags") or []:
            tag = str(raw or "").strip().lstrip("#")
            norm = normalize_text(tag).replace(" ", "")
            if not norm or norm in _GENERIC_HASHTAGS or len(norm) < 2:
                continue
            bonus = 2 if any("가" <= ch <= "힣" for ch in tag) else 0
            if "챌린지" in tag.lower() or "challenge" in tag.lower():
                bonus += 4
            counts[tag] += 1 + bonus

        music = item.get("musicInfo") or {}
        song = str(music.get("song_name") or music.get("songName") or "").strip()
        artist = str(music.get("artist_name") or music.get("artistName") or "").strip()
        if song and len(normalize_text(song)) >= 2:
            # Require repetition before using a song as a new search seed.
            music_counts[song] += 1
            if artist:
                music_counts[f"{artist} {song}"] += 1

    ranked: list[tuple[str, float]] = []
    for term, count in counts.items():
        ranked.append((term, float(count)))
    for term, count in music_counts.items():
        if count >= 2:
            ranked.append((term, float(count) * 1.6))

    ranked.sort(key=lambda item: item[1], reverse=True)
    result: list[str] = []
    seen: set[str] = set()
    for term, _ in ranked:
        norm = normalize_text(term)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        result.append(term)
        if len(result) >= max(0, int(max_terms)):
            break
    return result


def popular_item_to_evidence(item: dict[str, Any], *, seed: str = "") -> dict[str, Any]:
    music = item.get("musicInfo") or {}
    hashtags = [str(x) for x in (item.get("hashtags") or [])]
    caption = str(item.get("caption") or "")
    song = str(music.get("song_name") or music.get("songName") or "")
    artist = str(music.get("artist_name") or music.get("artistName") or "")
    audio_id = str(
        music.get("audio_id")
        or music.get("audioId")
        or music.get("audio_canonical_id")
        or ""
    )
    owner = str(item.get("ownerUsername") or item.get("username") or item.get("ownerId") or "")
    short_code = str(item.get("shortCode") or item.get("id") or "")
    url = str(item.get("url") or (f"https://www.instagram.com/reel/{short_code}/" if short_code else ""))
    text = " | ".join(
        [caption, "#" + " #".join(hashtags[:30]) if hashtags else "", song, artist]
    )
    return {
        "evidence_id": f"ig:{short_code or abs(hash(url))}",
        "source": "instagram_popular",
        "title": caption[:180],
        "text": text[:1200],
        "published_at": item.get("timestamp") or "",
        "url": url,
        "author": owner,
        "views": max(
            safe_int(item.get("videoViewCount")),
            safe_int(item.get("videoPlayCount")),
            safe_int(item.get("viewCount")),
        ),
        "likes": safe_int(item.get("likesCount") or item.get("likeCount")),
        "comments": safe_int(item.get("commentsCount") or item.get("commentCount")),
        "shares": safe_int(item.get("sharesCount") or item.get("shareCount")),
        "seed": seed or extract_search_term(item.get("inputUrl")),
        "kr_ratio": korean_ratio(text),
        "audio_id": audio_id,
        "song_name": song,
        "artist_name": artist,
        "hashtags": hashtags,
    }


class InstagramApifyConnector:
    source = "instagram_apify"

    def __init__(self, config: dict[str, Any], timezone_name: str = "Asia/Seoul") -> None:
        self.config = config
        self.timezone_name = timezone_name

    def collect(self, candidates: pd.DataFrame, now: pd.Timestamp) -> ConnectorResult:
        token_env = str(self.config.get("api_token_env", "APIFY_API_TOKEN"))
        token = os.getenv(token_env, "").strip()
        if not token:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame(),
                status={"enabled": True, "success": False, "reason": f"{token_env}가 없습니다."},
                raw_rows=pd.DataFrame(),
            )

        per_challenge = max(5, int(self.config.get("results_per_challenge", 35)))
        max_terms_per_challenge = max(1, int(self.config.get("max_terms_per_challenge", 2)))
        timeout_seconds = max(60, int(self.config.get("timeout_seconds", 240)))

        term_to_candidate: dict[str, str] = {}
        terms: list[str] = []
        max_challenges = max(1, int(self.config.get("max_challenges", len(candidates))))
        candidate_subset = candidates.head(max_challenges)
        for row in candidate_subset.itertuples(index=False):
            aliases = list(getattr(row, "alias_list", []) or [str(row.name)])
            local_terms: list[str] = []
            for alias in aliases:
                compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", str(alias).lstrip("#"))
                if len(compact) >= 2:
                    local_terms.append(compact)
            if not local_terms:
                local_terms = [re.sub(r"\s+", "", str(row.name))]
            for term in list(dict.fromkeys(local_terms))[:max_terms_per_challenge]:
                norm = normalize_text(term).replace(" ", "")
                if norm:
                    term_to_candidate[norm] = str(row.challenge_id)
                    terms.append(term)

        if not terms:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame(),
                status={"enabled": True, "success": True, "rows": 0, "reason": "검색어 없음"},
                raw_rows=pd.DataFrame(),
            )

        try:
            items = run_actor_items(
                token=token,
                actor_id=str(self.config.get("hashtag_actor_id", HASHTAG_ACTOR)),
                run_input={
                    "hashtags": list(dict.fromkeys(terms)),
                    "keywordSearch": False,
                    "resultsType": "reels",
                    "resultsLimit": per_challenge,
                },
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:
            return ConnectorResult(
                source=self.source,
                metrics=pd.DataFrame(),
                status={"enabled": True, "success": False, "error": str(exc), "reason": "Apify Instagram Hashtag Scraper 호출 실패"},
                raw_rows=pd.DataFrame(),
            )

        rows: list[dict[str, Any]] = []
        unmatched = 0
        for item in items:
            challenge_id = _resolve_candidate(item, term_to_candidate, candidates)
            if not challenge_id:
                unmatched += 1
                continue
            rows.append(_item_to_observation(item, challenge_id))

        frame = pd.DataFrame(rows)
        if frame.empty:
            metrics = aggregate_content_rows(candidates, pd.DataFrame(), now, prefix="instagram")
        else:
            frame = frame.drop_duplicates(subset=["challenge_id", "content_id"], keep="first")
            metrics = aggregate_content_rows(candidates, frame, now, prefix="instagram")

        audio_stats = _audio_reuse_metrics(candidates, frame)
        metrics = metrics.merge(audio_stats, on="challenge_id", how="left")
        for col in ("instagram_audio_reuse_ratio", "instagram_unique_audio_7d"):
            if col not in metrics.columns:
                metrics[col] = 0.0
            metrics[col] = pd.to_numeric(metrics[col], errors="coerce").fillna(0.0)

        return ConnectorResult(
            source=self.source,
            metrics=metrics,
            raw_rows=frame,
            status={
                "enabled": True,
                "success": True,
                "actor": str(self.config.get("hashtag_actor_id", HASHTAG_ACTOR)),
                "terms": len(set(terms)),
                "actor_rows": len(items),
                "matched_rows": len(frame),
                "unmatched_rows": unmatched,
            },
        )


def _item_to_observation(item: dict[str, Any], challenge_id: str) -> dict[str, Any]:
    music = item.get("musicInfo") or {}
    caption = str(item.get("caption") or "")
    hashtags = [str(x) for x in (item.get("hashtags") or [])]
    location = str(item.get("locationName") or "")
    text = " ".join([caption, " ".join(hashtags), location])
    kr = clip01(max(korean_ratio(text), 0.75 if any("가" <= ch <= "힣" for ch in location) else 0.0))
    audio_id = str(
        music.get("audio_id")
        or music.get("audioId")
        or music.get("audio_canonical_id")
        or ""
    )
    views = max(
        safe_int(item.get("videoPlayCount")),
        safe_int(item.get("videoViewCount")),
        safe_int(item.get("viewCount")),
        safe_int(item.get("playCount")),
    )
    owner = str(item.get("ownerUsername") or item.get("ownerId") or "unknown")
    return {
        "challenge_id": challenge_id,
        "challenge_name": "",
        "platform": "instagram",
        "content_id": str(item.get("shortCode") or item.get("id") or item.get("url") or ""),
        "author_id": owner,
        "created_at": item.get("timestamp") or "",
        "caption": caption,
        "hashtags": " ".join(hashtags),
        "audio_id": audio_id,
        "effect_id": "",
        "template_id": "",
        "views": views,
        "likes": safe_int(item.get("likesCount") or item.get("likeCount")),
        "comments": safe_int(item.get("commentsCount") or item.get("commentCount")),
        "shares": safe_int(item.get("sharesCount") or item.get("shareCount") or item.get("reshareCount")),
        "is_paid": parse_bool(item.get("isSponsored") or item.get("isPaidPartnership")),
        "kr_affinity": kr,
        "creator_followers": safe_int(item.get("ownerFollowersCount") or item.get("followersCount")),
        "creator_category": "instagram_creator",
        "instagram_url": str(item.get("url") or ""),
        "song_name": str(music.get("song_name") or ""),
    }


def _resolve_candidate(
    item: dict[str, Any], term_to_candidate: dict[str, str], candidates: pd.DataFrame
) -> str:
    term = normalize_text(extract_search_term(item.get("inputUrl"))).replace(" ", "")
    if term in term_to_candidate:
        return term_to_candidate[term]

    text = normalize_text(
        " ".join(
            [
                str(item.get("caption") or ""),
                " ".join(str(x) for x in (item.get("hashtags") or [])),
            ]
        )
    )
    matches: list[str] = []
    for row in candidates.itertuples(index=False):
        aliases = list(getattr(row, "alias_norms", []) or [])
        if any(alias and alias in text for alias in aliases):
            matches.append(str(row.challenge_id))
    return matches[0] if len(set(matches)) == 1 else ""


def _audio_reuse_metrics(candidates: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    now = pd.Timestamp.now(tz="UTC")
    for row in candidates.itertuples(index=False):
        if frame.empty:
            group = frame
        else:
            group = frame[frame["challenge_id"] == row.challenge_id].copy()
        if group.empty:
            rows.append({"challenge_id": row.challenge_id, "instagram_audio_reuse_ratio": 0.0, "instagram_unique_audio_7d": 0.0})
            continue
        created = pd.to_datetime(group["created_at"], utc=True, errors="coerce")
        recent = group[(created >= now - pd.Timedelta(days=7)) & created.notna()].copy()
        if recent.empty:
            recent = group.copy()
        ids = recent["audio_id"].fillna("").astype(str)
        ids = ids[ids.ne("")]
        if ids.empty:
            reuse = 0.0
            unique = 0.0
        else:
            counts = ids.value_counts()
            reuse = float(counts.iloc[0]) / max(1.0, float(len(recent)))
            unique = float(ids.nunique())
        rows.append({"challenge_id": row.challenge_id, "instagram_audio_reuse_ratio": clip01(reuse), "instagram_unique_audio_7d": unique})
    return pd.DataFrame(rows)


def extract_search_term(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        parsed = urlparse(text)
        path = unquote(parsed.path)
        for marker in ("/popular/", "/tags/"):
            if marker in path:
                return path.split(marker, 1)[1].strip("/")
    except Exception:
        return ""
    return ""


def clean_search_term(value: Any) -> str:
    text = str(value or "").strip().replace(",", " ")
    text = re.sub(r"[!?.,:;\-+=*&%$#@/\\~^|<>()[\]{}\"'`]", " ", text)
    return re.sub(r"\s+", " ", text).strip()
