from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from .connectors.naver import (
    NAVER_API_HUB_BASE_URL,
    NAVER_API_HUB_CLIENT_ID_HEADER,
    NAVER_API_HUB_CLIENT_SECRET_HEADER,
)
from .io import CANDIDATE_COLUMNS, write_csv_atomic
from .connectors.apify_instagram import collect_popular_reels_resilient, derive_expansion_terms, popular_item_to_evidence
from .gemini_json import call_gemini_structured
from .utils import chunks, korean_ratio, normalize_text, request_json, stable_hash, strip_html


@dataclass
class AutoDiscoveryResult:
    candidates: pd.DataFrame
    evidence: pd.DataFrame
    status: dict[str, Any]


DEFAULT_INSTAGRAM_SEEDS = [
    "챌린지", "댄스", "춤", "댄스챌린지", "릴스", "유행", "요즘유행", "밈", "밈챌린지",
    "포즈", "사진포즈", "손댄스", "손동작", "안무", "커플", "커플릴스", "친구", "친구릴스",
    "변신", "전환", "메이크업", "메이크업챌린지", "립싱크", "표정", "KPOP", "케이팝",
    "dancechallenge", "challenge", "reelschallenge", "choreography",
]

DEFAULT_YOUTUBE_SEEDS = [
    "챌린지",
    "댄스 챌린지",
    "유행 챌린지",
    "쇼츠 챌린지",
    "밈 챌린지",
    "포즈 챌린지",
    "커플 챌린지",
    "친구 챌린지",
    "운동 챌린지",
    "레시피 챌린지",
    "메이크업 챌린지",
    "KPOP 챌린지",
]

DEFAULT_NAVER_SEEDS = [
    "챌린지",
    "댄스 챌린지",
    "유행 챌린지",
    "쇼츠 챌린지",
    "밈 챌린지",
    "SNS 챌린지",
]


def discover_live_challenges(
    config: dict[str, Any], now: pd.Timestamp, *, output_path: str | None = None
) -> AutoDiscoveryResult:
    """Discover Korean challenge candidates from APIs without any human seed list.

    Required: YOUTUBE_API_KEY and GEMINI_API_KEY (env names are configurable).
    Optional but strongly recommended: NAVER API HUB credentials.
    """

    gemini_env = str(config.get("gemini_api_key_env", "GEMINI_API_KEY"))
    youtube_env = str(config.get("youtube_api_key_env", "YOUTUBE_API_KEY"))
    gemini_key = os.getenv(gemini_env, "").strip()
    youtube_key = os.getenv(youtube_env, "").strip()
    if not gemini_key:
        raise RuntimeError(f"자동 후보 발굴에 필요한 환경변수 {gemini_env}가 없습니다.")
    if not youtube_key:
        raise RuntimeError(f"대표 영상 탐색에 필요한 환경변수 {youtube_env}가 없습니다.")

    session = requests.Session()
    session.headers.update({"User-Agent": "challenge-ranker-auto/1.0"})

    instagram_cfg = config.get("instagram_apify", {})
    apify_records, apify_status = _collect_instagram_seed_corpus(instagram_cfg, now)
    if bool(instagram_cfg.get("required", False)) and not apify_records:
        reason = apify_status.get("reason") or apify_status.get("error") or "Instagram popular reels 수집 결과가 없습니다."
        raise RuntimeError(f"Instagram/Apify Discovery 실패: {reason}")

    youtube_cfg = dict(config.get("youtube", {}))
    if not apify_records and bool(youtube_cfg.get("fallback_when_instagram_empty", True)):
        fallback_seeds = youtube_cfg.get("fallback_seed_queries") or DEFAULT_YOUTUBE_SEEDS
        youtube_cfg["seed_queries"] = list(fallback_seeds)
        youtube_cfg["max_search_requests"] = int(youtube_cfg.get("fallback_max_search_requests", 12))
        youtube_cfg["viewcount_seed_count"] = int(youtube_cfg.get("fallback_viewcount_seed_count", 2))
        apify_status["degraded_mode"] = True
        apify_status["fallback"] = "YouTube/NAVER discovery activated because Instagram popular reels returned no usable rows."

    youtube_records, yt_status = _collect_youtube_seed_corpus(
        session, youtube_key, youtube_cfg, now
    )
    naver_records, naver_status = _collect_naver_seed_corpus(
        session, config.get("naver", {}), now
    )

    evidence_records = apify_records + youtube_records + naver_records
    if not evidence_records:
        raise RuntimeError("Instagram/YouTube/네이버에서 후보 발굴용 원시 데이터를 가져오지 못했습니다.")

    evidence = pd.DataFrame(evidence_records)
    evidence = _prioritize_evidence(
        evidence, max_records=int(config.get("max_evidence_records", 320)), now=now
    )

    model = str(config.get("model", os.getenv("GEMINI_MODEL", "auto")))
    chunk_size = max(20, int(config.get("ai_chunk_size", 80)))

    extracted: list[dict[str, Any]] = []
    for chunk in chunks(evidence.to_dict(orient="records"), chunk_size):
        parsed = _extract_chunk(gemini_key, model, list(chunk), now)
        extracted.extend(parsed.get("challenges", []))

    if not extracted:
        raise RuntimeError("AI가 원시 데이터에서 유효한 챌린지 후보를 찾지 못했습니다.")

    canonical = _merge_candidates(gemini_key, model, extracted)
    max_candidates = max(100, int(config.get("max_candidates", 180)))
    candidates = _canonical_to_frame(
        canonical,
        evidence,
        now,
        max_candidates=max_candidates,
        min_confidence=float(config.get("min_candidate_confidence", 0.12)),
    )

    # Recall supplement: when Gemini merges too aggressively, use only observed
    # Instagram hashtag/audio metadata to recover additional candidates. These are
    # not invented facts; they are low-confidence entities backed by source rows
    # and are still revalidated by Instagram/NAVER/YouTube + final Gemini review.
    supplemental = _supplement_candidates_from_evidence(
        evidence,
        now,
        existing=candidates,
        max_total=max_candidates,
    )
    if not supplemental.empty:
        candidates = pd.concat([candidates, supplemental], ignore_index=True, sort=False)
        candidates = candidates.drop_duplicates(subset=["challenge_id"], keep="first").head(max_candidates)
    if candidates.empty:
        raise RuntimeError("AI 후보 병합 후 남은 유효 챌린지가 없습니다.")

    if output_path:
        write_csv_atomic(candidates[CANDIDATE_COLUMNS], output_path)

    status = {
        "enabled": True,
        "success": True,
        "instagram_apify": apify_status,
        "youtube": yt_status,
        "naver": naver_status,
        "evidence_rows": int(len(evidence)),
        "raw_ai_candidates": int(len(extracted)),
        "canonical_candidates": int(len(candidates)),
        "supplemental_candidates": int(len(supplemental)) if "supplemental" in locals() else 0,
        "model": model,
    }
    return AutoDiscoveryResult(candidates=candidates, evidence=evidence, status=status)



def _collect_instagram_seed_corpus(
    cfg: dict[str, Any],
    now: pd.Timestamp,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    token_env = str(cfg.get("api_token_env", "APIFY_API_TOKEN"))
    token = os.getenv(token_env, "").strip()
    if not token:
        return [], {"success": False, "skipped": True, "reason": f"{token_env}가 없습니다."}

    seeds = [str(x).strip() for x in cfg.get("seed_queries", DEFAULT_INSTAGRAM_SEEDS) if str(x).strip()]
    search_limit = max(5, int(cfg.get("search_limit_per_seed", 24)))
    timeout_seconds = max(60, int(cfg.get("timeout_seconds", 240)))
    max_seed_runs = max(1, int(cfg.get("max_seed_runs", 10)))
    max_expansion_runs = max(0, int(cfg.get("max_expansion_runs", 8)))

    first, first_report = collect_popular_reels_resilient(
        token=token,
        seeds=seeds,
        search_limit=search_limit,
        timeout_seconds=timeout_seconds,
        max_seed_runs=max_seed_runs,
    )
    expansion = []
    if cfg.get("expand_from_hashtags", True) and first:
        expansion = derive_expansion_terms(first, int(cfg.get("max_expansion_terms", 8)))

    second: list[dict[str, Any]] = []
    second_report: dict[str, Any] = {"attempted_terms": 0, "successful_terms": 0, "failed": [], "rows": 0}
    if expansion and max_expansion_runs > 0:
        second, second_report = collect_popular_reels_resilient(
            token=token,
            seeds=expansion,
            search_limit=max(5, min(search_limit, int(cfg.get("expansion_search_limit", 24)))),
            timeout_seconds=timeout_seconds,
            max_seed_runs=max_expansion_runs,
        )

    by_id: dict[str, dict[str, Any]] = {}
    for item in [*first, *second]:
        evidence = popular_item_to_evidence(item)
        key = str(evidence.get("evidence_id"))
        by_id[key] = evidence
    records = list(by_id.values())
    success = bool(records)
    failed_samples = (first_report.get("failed") or [])[:4]
    reason = None
    if not success:
        reason = "Instagram popular reels에서 유효한 결과를 얻지 못했습니다. 일부 키워드는 popular feed가 없거나 Instagram이 Actor 요청을 일시 차단할 수 있습니다."
    return records, {
        "success": success,
        "actor": "apify/instagram-search-scraper",
        "seed_terms": len(seeds),
        "expansion_terms": expansion,
        "first_rows": len(first),
        "second_rows": len(second),
        "rows": len(records),
        "first_report": first_report,
        "second_report": second_report,
        "failed_samples": failed_samples,
        "reason": reason,
    }

def _collect_youtube_seed_corpus(
    session: requests.Session,
    api_key: str,
    cfg: dict[str, Any],
    now: pd.Timestamp,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    search_url = "https://www.googleapis.com/youtube/v3/search"
    videos_url = "https://www.googleapis.com/youtube/v3/videos"
    seeds = [str(x).strip() for x in cfg.get("seed_queries", DEFAULT_YOUTUBE_SEEDS) if str(x).strip()]
    lookback_days = max(2, int(cfg.get("lookback_days", 10)))
    max_results = min(50, max(5, int(cfg.get("max_results_per_query", 25))))
    viewcount_seed_count = max(0, int(cfg.get("viewcount_seed_count", 8)))
    max_search_requests = max(1, int(cfg.get("max_search_requests", 24)))
    published_after = (now - pd.Timedelta(days=lookback_days)).isoformat().replace("+00:00", "Z")

    video_search_hits: dict[str, set[str]] = {}
    requests_used = 0
    for index, seed in enumerate(seeds):
        orders = ["date"]
        if index < viewcount_seed_count:
            orders.append("viewCount")
        for order in orders:
            if requests_used >= max_search_requests:
                break
            payload = request_json(
                session,
                "GET",
                search_url,
                params={
                    "key": api_key,
                    "part": "snippet",
                    "type": "video",
                    "q": seed,
                    "order": order,
                    "publishedAfter": published_after,
                    "maxResults": max_results,
                    "videoDuration": "short",
                    "regionCode": cfg.get("region_code", "KR"),
                    "relevanceLanguage": cfg.get("relevance_language", "ko"),
                    "safeSearch": "moderate",
                },
            )
            requests_used += 1
            for item in payload.get("items", []):
                video_id = str(item.get("id", {}).get("videoId", "")).strip()
                if video_id:
                    video_search_hits.setdefault(video_id, set()).add(seed)
        if requests_used >= max_search_requests:
            break

    if not video_search_hits:
        return [], {"success": True, "search_requests": requests_used, "rows": 0}

    details: dict[str, dict[str, Any]] = {}
    detail_requests = 0
    for batch in chunks(list(video_search_hits), 50):
        payload = request_json(
            session,
            "GET",
            videos_url,
            params={
                "key": api_key,
                "part": "snippet,statistics,contentDetails",
                "id": ",".join(batch),
            },
        )
        detail_requests += 1
        for item in payload.get("items", []):
            details[str(item.get("id", ""))] = item

    records: list[dict[str, Any]] = []
    for video_id, item in details.items():
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        title = str(snippet.get("title", ""))
        description = str(snippet.get("description", ""))
        tags = [str(t) for t in (snippet.get("tags", []) or [])]
        text = " | ".join([title, description[:500], " ".join(tags[:25])])
        records.append(
            {
                "evidence_id": f"yt:{video_id}",
                "source": "youtube",
                "title": title,
                "text": text,
                "published_at": snippet.get("publishedAt", ""),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "author": str(snippet.get("channelTitle", "")),
                "views": int(stats.get("viewCount", 0) or 0),
                "likes": int(stats.get("likeCount", 0) or 0),
                "comments": int(stats.get("commentCount", 0) or 0),
                "seed": "|".join(sorted(video_search_hits.get(video_id, set()))),
                "kr_ratio": korean_ratio(text),
            }
        )
    return records, {
        "success": True,
        "search_requests": requests_used,
        "detail_requests": detail_requests,
        "rows": len(records),
    }


def _collect_naver_seed_corpus(
    session: requests.Session,
    cfg: dict[str, Any],
    now: pd.Timestamp,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    id_env = str(cfg.get("client_id_env", "NAVER_API_HUB_CLIENT_ID"))
    secret_env = str(cfg.get("client_secret_env", "NAVER_API_HUB_CLIENT_SECRET"))
    client_id = os.getenv(id_env, "").strip()
    client_secret = os.getenv(secret_env, "").strip()
    if not client_id or not client_secret:
        return [], {
            "success": False,
            "skipped": True,
            "reason": f"{id_env}/{secret_env}가 없어 네이버 원천 후보 발굴은 건너뜁니다.",
        }

    headers = {
        NAVER_API_HUB_CLIENT_ID_HEADER: client_id,
        NAVER_API_HUB_CLIENT_SECRET_HEADER: client_secret,
    }
    seeds = [str(x).strip() for x in cfg.get("seed_queries", DEFAULT_NAVER_SEEDS) if str(x).strip()]
    display = min(100, max(10, int(cfg.get("display", 60))))
    sources = cfg.get("sources", ["blog", "news"])
    records: list[dict[str, Any]] = []
    requests_used = 0

    for source in sources:
        if source not in {"blog", "news"}:
            continue
        url = f"{NAVER_API_HUB_BASE_URL}/search/v1/{source}"
        for seed in seeds:
            try:
                payload = request_json(
                    session,
                    "GET",
                    url,
                    headers=headers,
                    params={
                        "query": seed,
                        "display": display,
                        "start": 1,
                        "sort": "date",
                        "format": "json",
                    },
                )
                requests_used += 1
            except Exception as exc:
                # NAVER는 국내성 검증을 강화하는 보조 소스입니다.
                # 인증/권한 문제가 있더라도 YouTube + Gemini 기반 후보 발굴은 계속합니다.
                return records, {
                    "success": False,
                    "skipped": False,
                    "error": str(exc),
                    "requests": requests_used,
                    "rows": len(records),
                    "reason": (
                        "NAVER API HUB 호출에 실패했습니다. Client ID/Secret과 "
                        "Application에서 News/Blog/Search keyword trends API 선택 여부를 확인하세요."
                    ),
                }
            for idx, item in enumerate(payload.get("items", [])):
                title = strip_html(item.get("title", ""))
                description = strip_html(item.get("description", ""))
                if source == "blog":
                    published_at = str(item.get("postdate", ""))
                    url_value = str(item.get("link", ""))
                    author = str(item.get("bloggername", ""))
                else:
                    published_at = str(item.get("pubDate", ""))
                    url_value = str(item.get("originallink") or item.get("link") or "")
                    author = ""
                evidence_key = stable_hash(f"{source}|{url_value}|{title}|{published_at}", 16)
                text = f"{title} | {description}"
                records.append(
                    {
                        "evidence_id": f"nv:{source}:{evidence_key}",
                        "source": f"naver_{source}",
                        "title": title,
                        "text": text,
                        "published_at": published_at,
                        "url": url_value,
                        "author": author,
                        "views": 0,
                        "likes": 0,
                        "comments": 0,
                        "seed": seed,
                        "kr_ratio": korean_ratio(text),
                    }
                )

    # dedupe URL/title collisions from multiple seed queries
    if records:
        frame = pd.DataFrame(records).drop_duplicates(subset=["evidence_id"], keep="first")
        records = frame.to_dict(orient="records")
    return records, {"success": True, "requests": requests_used, "rows": len(records)}


def _prioritize_evidence(frame: pd.DataFrame, max_records: int, now: pd.Timestamp) -> pd.DataFrame:
    if frame.empty:
        return frame
    work = frame.copy()
    work["views"] = pd.to_numeric(work.get("views", 0), errors="coerce").fillna(0).clip(lower=0)
    work["kr_ratio"] = pd.to_numeric(work.get("kr_ratio", 0), errors="coerce").fillna(0).clip(0, 1)
    work["published_ts"] = work["published_at"].apply(_parse_any_datetime)
    age_days = (now - work["published_ts"]).dt.total_seconds() / 86400.0
    age_days = age_days.fillna(30).clip(lower=0)
    source_bonus = work["source"].map({"instagram_popular": 1.55, "youtube": 0.75, "naver_blog": 0.60, "naver_news": 0.35}).fillna(0.4)
    work["priority"] = (
        0.45 * work["views"].map(lambda v: math.log1p(v))
        + 2.5 * work["kr_ratio"]
        + 2.0 * (-age_days / 7.0).map(math.exp)
        + source_bonus
    )

    # Preserve source diversity before filling by global priority.
    selected: list[pd.DataFrame] = []
    per_source_floor = max(10, max_records // max(1, work["source"].nunique()) // 2)
    for _, group in work.groupby("source", sort=False):
        selected.append(group.nlargest(min(per_source_floor, len(group)), "priority"))
    seed = pd.concat(selected, ignore_index=False).drop_duplicates(subset=["evidence_id"])
    remaining = work[~work["evidence_id"].isin(seed["evidence_id"])]
    need = max(0, max_records - len(seed))
    final = pd.concat([seed, remaining.nlargest(need, "priority")], ignore_index=True)
    return final.head(max_records).drop(columns=["published_ts"], errors="ignore")


def _extract_chunk(
    api_key: str, model: str, records: list[dict[str, Any]], now: pd.Timestamp
) -> dict[str, Any]:
    lines = []
    for row in records:
        lines.append(
            "\t".join(
                [
                    str(row.get("evidence_id", "")),
                    f"source={row.get('source', '')}",
                    f"published={row.get('published_at', '')}",
                    f"views={int(row.get('views', 0) or 0)}",
                    f"seed={row.get('seed', '')}",
                    f"audio_id={row.get('audio_id', '')}",
                    f"music={_compact(str(row.get('artist_name', '')) + ' ' + str(row.get('song_name', '')), 120)}",
                    f"title={_compact(row.get('title', ''), 180)}",
                    f"text={_compact(row.get('text', ''), 420)}",
                ]
            )
        )
    prompt = f"""
오늘은 {now.tz_convert(ZoneInfo('Asia/Seoul')).date()}이다.
아래는 최근 Instagram popular reels를 중심으로 YouTube/NAVER를 보조 결합한 원시 증거다.

목표: '여러 사람이 같은 행동/댄스/포즈/밈 포맷/레시피/변신 포맷 등을 따라 만드는 SNS 챌린지' 후보만 추출한다.

판정 규칙:
- 단순히 제목에 '챌린지'가 있다는 이유만으로 후보로 만들지 않는다.
- 게임의 도전과제, TV/예능 제목, 기업 단독 이벤트, 스포츠 경기, 공부/30일 습관 같은 일반적 개인 목표는 제외한다.
- Discovery 단계는 recall을 우선한다. 반복 참여 증거가 약하지만 명시적 챌린지명/해시태그/음원 반복 신호가 있으면 낮은 confidence(0.15~0.4)로 후보에 남긴다.
- 같은 유행의 표기 차이/해시태그/줄임말은 aliases에 모은다.
- Instagram에서 같은 audio_id가 여러 독립 creator에게 반복되면 강한 후보 신호로 보되, 같은 음악만 쓰고 행동/포맷이 다르면 합치지 않는다.
- Instagram popular reels 증거를 후보 발견의 최우선 신호로 보고, YouTube는 교차 플랫폼 확인, NAVER는 국내 언어/검색 확산 보조 신호로 사용한다.
- evidence_ids는 반드시 아래 입력에 실제 존재하는 ID만 사용한다.
- kr_relevance는 한국 이용자/한국어/한국 크리에이터 생태계에서 유행 중이라는 증거의 강도를 0~1로 판단한다.
- confidence는 이것이 실제 참여형 챌린지 엔티티라는 확신 0~1이다.
- 이름은 한국에서 사람들이 알아볼 수 있는 가장 간결한 canonical name으로 적는다.
- 한 배치 안에서 근거가 있는 서로 다른 챌린지를 누락하지 말고 폭넓게 추출한다. 유효 후보가 많으면 최대 35~40개까지 반환한다.
- 근거가 없으면 빈 challenges를 반환한다. 추측으로 챌린지를 만들지 않는다.

원시 증거:
{chr(10).join(lines)}
""".strip()

    return call_gemini_structured(
        api_key=api_key,
        model=model,
        system_prompt="당신은 한국 SNS 트렌드 데이터 분석가다. 제공된 최신 증거만 사용하고, 참여형 챌린지를 보수적으로 추출한다.",
        user_prompt=prompt,
        schema_name="challenge_extraction",
        schema=_challenge_batch_schema(),
    )


def _merge_candidates(
    api_key: str, model: str, extracted: list[dict[str, Any]]
) -> dict[str, Any]:
    rows = []
    for idx, item in enumerate(extracted):
        rows.append(
            {
                "candidate_index": idx,
                "name": item.get("name", ""),
                "aliases": item.get("aliases", []),
                "category": item.get("category", ""),
                "evidence_ids": item.get("evidence_ids", []),
                "confidence": item.get("confidence", 0.0),
                "kr_relevance": item.get("kr_relevance", 0.0),
                "reason": item.get("reason", ""),
            }
        )
    prompt = f"""
다음은 여러 배치에서 독립적으로 추출한 한국 SNS 챌린지 후보다.
동일한 실제 참여형 챌린지만 하나로 병합하고 canonical name/aliases/evidence_ids를 정리하라.

중요:
- 비슷한 이름이어도 행동/포맷이 다르면 합치지 않는다.
- 같은 음원만 공유하고 다른 포맷이면 합치지 않는다.
- aliases와 evidence_ids는 중복 제거한다.
- confidence와 kr_relevance는 여러 증거의 일관성을 반영해 0~1로 재평가한다.
- 명백히 챌린지가 아닌 항목만 제거한다. 애매하지만 source evidence가 있는 후보는 낮은 confidence로 보존하고 최종 판정 단계에 넘긴다.
- 서로 다른 유효 챌린지는 최대한 보존한다. 이름이 비슷하다는 이유만으로 과도하게 합치지 않는다.
- 새 사실이나 입력에 없는 챌린지를 만들지 않는다.

후보 JSON:
{rows}
""".strip()
    return call_gemini_structured(
        api_key=api_key,
        model=model,
        system_prompt="당신은 SNS 트렌드 엔티티 리졸루션 전문가다. 후보를 과도하게 합치지 말고 증거 중심으로 중복을 정리한다.",
        user_prompt=prompt,
        schema_name="challenge_merge",
        schema=_challenge_batch_schema(),
    )


def _canonical_to_frame(
    canonical: dict[str, Any],
    evidence: pd.DataFrame,
    now: pd.Timestamp,
    *,
    max_candidates: int,
    min_confidence: float,
) -> pd.DataFrame:
    evidence_dates: dict[str, pd.Timestamp] = {
        str(row.evidence_id): _parse_any_datetime(row.published_at)
        for row in evidence.itertuples(index=False)
    }
    evidence_views: dict[str, float] = {
        str(row.evidence_id): float(getattr(row, "views", 0) or 0)
        for row in evidence.itertuples(index=False)
    }

    records: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for item in canonical.get("challenges", []):
        name = str(item.get("name", "")).strip()
        norm = normalize_text(name)
        if not name or not norm or norm in seen_names:
            continue
        confidence = min(1.0, max(0.0, float(item.get("confidence", 0.0) or 0.0)))
        kr_relevance = min(1.0, max(0.0, float(item.get("kr_relevance", 0.0) or 0.0)))
        if confidence < min_confidence:
            continue
        valid_evidence = [eid for eid in item.get("evidence_ids", []) if eid in evidence_dates]
        dates = [evidence_dates[eid] for eid in valid_evidence if not pd.isna(evidence_dates[eid])]
        discovered_at = min(dates) if dates else now
        aliases = _unique_aliases([name, *item.get("aliases", [])])[:20]
        support = len(set(valid_evidence))
        view_support = sum(math.log1p(max(0.0, evidence_views.get(eid, 0.0))) for eid in valid_evidence)
        pre_score = 3.0 * confidence + 2.0 * kr_relevance + 0.35 * support + 0.02 * view_support
        records.append(
            {
                "challenge_id": f"auto_{stable_hash(norm, 14)}",
                "name": name,
                "aliases": "|".join(aliases),
                "category": str(item.get("category", "")).strip() or "기타",
                "discovered_at": discovered_at,
                "kr_affinity_hint": kr_relevance,
                "entity_confidence": confidence,
                "_support": support,
                "_pre_score": pre_score,
                "_ai_reason": str(item.get("reason", "")),
            }
        )
        seen_names.add(norm)

    if not records:
        return pd.DataFrame(columns=CANDIDATE_COLUMNS + ["alias_list", "alias_norms"])
    frame = pd.DataFrame(records).sort_values(
        ["_pre_score", "_support"], ascending=[False, False]
    ).head(max_candidates)
    frame["alias_list"] = frame["aliases"].apply(lambda value: _unique_aliases(str(value).split("|")))
    frame["alias_norms"] = frame["alias_list"].apply(
        lambda values: [normalize_text(v) for v in values if normalize_text(v)]
    )
    return frame.reset_index(drop=True)



def _supplement_candidates_from_evidence(
    evidence: pd.DataFrame,
    now: pd.Timestamp,
    *,
    existing: pd.DataFrame,
    max_total: int,
) -> pd.DataFrame:
    """Recover Instagram-native candidates directly from observed hashtags/audio.

    This is a recall layer, not a final truth layer. It only uses source metadata,
    assigns conservative confidence, and later platform/AI validation can demote
    or reject the candidate.
    """
    if evidence.empty or len(existing) >= max_total:
        return pd.DataFrame(columns=list(existing.columns) if not existing.empty else CANDIDATE_COLUMNS + ["alias_list", "alias_norms"])

    instagram = evidence[evidence["source"].fillna("").astype(str).str.startswith("instagram")].copy()
    if instagram.empty:
        return pd.DataFrame(columns=list(existing.columns) if not existing.empty else CANDIDATE_COLUMNS + ["alias_list", "alias_norms"])

    existing_norms = {normalize_text(str(x)) for x in existing.get("name", pd.Series(dtype=str)).tolist()}
    records: list[dict[str, Any]] = []

    # 1) Explicit challenge-like hashtags. These are the safest automatic entities.
    tag_stats: dict[str, dict[str, Any]] = {}
    for row in instagram.to_dict(orient="records"):
        hashtags = row.get("hashtags") or []
        if isinstance(hashtags, str):
            hashtags = [x.strip().lstrip("#") for x in re.split(r"[|,\s]+", hashtags) if x.strip()]
        for raw in hashtags:
            tag = str(raw or "").strip().lstrip("#")
            norm = normalize_text(tag).replace(" ", "")
            if len(norm) < 3:
                continue
            if "챌린지" not in norm and "challenge" not in norm:
                continue
            stat = tag_stats.setdefault(norm, {"tag": tag, "evidence": set(), "authors": set(), "views": 0.0})
            stat["evidence"].add(str(row.get("evidence_id", "")))
            stat["authors"].add(str(row.get("author", "")))
            stat["views"] += float(row.get("views", 0) or 0)

    for norm, stat in sorted(tag_stats.items(), key=lambda kv: (len(kv[1]["authors"]), kv[1]["views"]), reverse=True):
        name = str(stat["tag"]).replace("_", " ").strip()
        if "challenge" in name.lower() and "챌린지" not in name:
            name = name.replace("challenge", "챌린지").replace("Challenge", "챌린지")
        name_norm = normalize_text(name)
        if not name_norm or name_norm in existing_norms:
            continue
        authors = len({x for x in stat["authors"] if x})
        support = len({x for x in stat["evidence"] if x})
        confidence = min(0.72, 0.24 + 0.06 * min(6, max(authors, support)))
        records.append({
            "challenge_id": f"auto_{stable_hash(name_norm, 14)}",
            "name": name,
            "aliases": name,
            "category": "Instagram hashtag",
            "discovered_at": now,
            "kr_affinity_hint": 0.75 if any("가" <= ch <= "힣" for ch in name) else 0.35,
            "entity_confidence": confidence,
            "_support": support,
            "_pre_score": confidence * 3 + math.log1p(stat["views"]) * 0.03 + authors * 0.12,
            "_ai_reason": "Instagram explicit challenge hashtag supplement",
            "alias_list": [name],
            "alias_norms": [name_norm],
        })
        existing_norms.add(name_norm)

    # 2) Repeated Instagram audio/song. Lower confidence because one song can
    # contain multiple formats, but it is useful for name-less early Reel trends.
    audio_stats: dict[str, dict[str, Any]] = {}
    for row in instagram.to_dict(orient="records"):
        audio_id = str(row.get("audio_id") or "").strip()
        song = str(row.get("song_name") or "").strip()
        if not audio_id or not song or len(normalize_text(song)) < 2:
            continue
        stat = audio_stats.setdefault(audio_id, {"song": song, "evidence": set(), "authors": set(), "views": 0.0})
        stat["evidence"].add(str(row.get("evidence_id", "")))
        stat["authors"].add(str(row.get("author", "")))
        stat["views"] += float(row.get("views", 0) or 0)

    for audio_id, stat in sorted(audio_stats.items(), key=lambda kv: (len(kv[1]["authors"]), kv[1]["views"]), reverse=True):
        authors = len({x for x in stat["authors"] if x})
        support = len({x for x in stat["evidence"] if x})
        if max(authors, support) < 2:
            continue
        name = f"{stat['song']} 챌린지".strip()
        name_norm = normalize_text(name)
        if not name_norm or name_norm in existing_norms:
            continue
        confidence = min(0.50, 0.16 + 0.045 * min(6, max(authors, support)))
        records.append({
            "challenge_id": f"auto_{stable_hash(name_norm + audio_id, 14)}",
            "name": name,
            "aliases": f"{name}|{stat['song']}",
            "category": "Instagram audio",
            "discovered_at": now,
            "kr_affinity_hint": 0.45,
            "entity_confidence": confidence,
            "_support": support,
            "_pre_score": confidence * 3 + math.log1p(stat["views"]) * 0.025 + authors * 0.10,
            "_ai_reason": f"Repeated Instagram audio_id={audio_id}",
            "alias_list": [name, stat["song"]],
            "alias_norms": [name_norm, normalize_text(stat["song"])],
        })
        existing_norms.add(name_norm)

    if not records:
        return pd.DataFrame(columns=list(existing.columns) if not existing.empty else CANDIDATE_COLUMNS + ["alias_list", "alias_norms"])

    frame = pd.DataFrame(records).sort_values(["_pre_score", "_support"], ascending=[False, False])
    remain = max(0, max_total - len(existing))
    return frame.head(remain).reset_index(drop=True)

def _challenge_batch_schema() -> dict[str, Any]:
    item = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "aliases": {"type": "array", "items": {"type": "string"}},
            "category": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
            "kr_relevance": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": [
            "name", "aliases", "category", "evidence_ids",
            "confidence", "kr_relevance", "reason"
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"challenges": {"type": "array", "items": item}},
        "required": ["challenges"],
        "additionalProperties": False,
    }


def _unique_aliases(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        key = normalize_text(value)
        if not value or not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _compact(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _parse_any_datetime(value: Any) -> pd.Timestamp:
    text = str(value or "").strip()
    if not text:
        return pd.NaT
    # NAVER blog postdate YYYYMMDD
    if len(text) == 8 and text.isdigit():
        ts = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
        if pd.isna(ts):
            return pd.NaT
        return pd.Timestamp(ts).tz_localize("Asia/Seoul").tz_convert("UTC")
    # NAVER news RFC2822
    try:
        parsed = parsedate_to_datetime(text)
        if parsed is not None:
            ts = pd.Timestamp(parsed)
            return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    except (TypeError, ValueError, OverflowError):
        pass
    ts = pd.to_datetime(text, utc=True, errors="coerce")
    return pd.Timestamp(ts) if not pd.isna(ts) else pd.NaT
