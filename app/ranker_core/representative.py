from __future__ import annotations

import math
import os
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd

from .gemini_json import call_gemini_structured
from .utils import clip01, korean_ratio, normalize_text, safe_float

YOUTUBE_PLATFORMS = {
    "youtube", "youtube_shorts", "youtube shorts", "youtube-short", "youtube-shorts",
    "shorts", "yt", "yt_shorts",
}

REPRESENTATIVE_COLUMNS = [
    "challenge_id",
    "representative_youtube_url",
    "representative_youtube_video_id",
    "representative_youtube_title",
    "representative_youtube_channel",
    "representative_youtube_published_at",
    "representative_youtube_views",
    "representative_youtube_score",
    "representative_youtube_source",
    "representative_youtube_participation_type",
    "guide_youtube_url",
    "guide_youtube_video_id",
    "guide_youtube_title",
    "guide_youtube_channel",
    "guide_youtube_published_at",
    "guide_youtube_views",
    "guide_youtube_score",
    "guide_youtube_source",
    "guide_youtube_type",
]

# 앱 화면용 대표 영상: 유명하고 많이 본 실제 챌린지 영상을 강하게 우선한다.
DEFAULT_REPRESENTATIVE_WEIGHTS = {
    "relevance": 0.25,
    "participation": 0.10,
    "popularity": 0.45,
    "recency": 0.05,
    "engagement": 0.05,
    "kr_affinity": 0.10,
}

# 따라하기용 가이드 영상: 안무/튜토리얼/거울모드/연습영상과 동작 명료도를 우선한다.
DEFAULT_GUIDE_WEIGHTS = {
    "relevance": 0.20,
    "guideability": 0.45,
    "participation": 0.10,
    "popularity": 0.05,
    "recency": 0.05,
    "engagement": 0.05,
    "kr_affinity": 0.10,
}

PARTICIPATION_SCORES = {
    "DIRECT_PARTICIPATION": 1.00,
    "ORIGINAL_OR_DEMO": 0.95,
    "REACTION": 0.30,
    "COMMENTARY": 0.10,
    "UNRELATED": 0.0,
}

GUIDE_SCORES = {
    "TUTORIAL_OR_MIRRORED": 1.00,
    "DANCE_PRACTICE_OR_CHOREO": 0.96,
    "CLEAR_DEMO": 0.86,
    "DIRECT_PARTICIPATION": 0.68,
    "PERFORMANCE_CUT": 0.42,
    "REACTION": 0.15,
    "COMMENTARY": 0.03,
    "UNRELATED": 0.0,
}

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def select_representative_youtube(
    candidates: pd.DataFrame,
    rows: pd.DataFrame | None,
    now: pd.Timestamp,
    config: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Select two different-purpose YouTube links per challenge.

    representative_youtube_url
        App-card video. Popularity/fame is the largest signal, while obvious
        commentary/unrelated videos are excluded.

    guide_youtube_url
        Follow-along video. Tutorial, mirrored, choreography, dance-practice and
        clear-demo metadata receive the largest weight. If no explicit guide is
        available, a clear direct-participation video is used as fallback.
    """
    config = config or {}
    now_utc = pd.Timestamp(now)
    now_utc = now_utc.tz_localize("UTC") if now_utc.tzinfo is None else now_utc.tz_convert("UTC")

    base = candidates[["challenge_id"]].copy()
    numeric_columns = {
        "representative_youtube_views", "representative_youtube_score",
        "guide_youtube_views", "guide_youtube_score",
    }
    for col in REPRESENTATIVE_COLUMNS[1:]:
        base[col] = 0.0 if col in numeric_columns else ""

    if not config.get("enabled", True) or rows is None or rows.empty:
        return base[REPRESENTATIVE_COLUMNS]

    frame = _prepare_rows(rows)
    if frame.empty:
        return base[REPRESENTATIVE_COLUMNS]

    representative_weights = _normalized_weights(
        config.get("representative_weights")
        or config.get("weights")
        or DEFAULT_REPRESENTATIVE_WEIGHTS,
        DEFAULT_REPRESENTATIVE_WEIGHTS,
    )
    guide_weights = _normalized_weights(
        config.get("guide_weights") or DEFAULT_GUIDE_WEIGHTS,
        DEFAULT_GUIDE_WEIGHTS,
    )

    paid_penalty = clip01(safe_float(config.get("paid_penalty", 0.08), 0.08))
    half_life = max(1.0, safe_float(config.get("recency_half_life_days", 35), 35))
    min_relevance = clip01(safe_float(config.get("minimum_relevance", 0.32), 0.32))
    fallback_min = clip01(safe_float(config.get("fallback_minimum_relevance", 0.14), 0.14))
    fallback_enabled = bool(config.get("fallback_enabled", True))

    candidate_map = {
        str(row.challenge_id): {
            "name": str(row.name),
            "aliases": list(getattr(row, "alias_list", []) or [str(row.name)]),
        }
        for row in candidates.itertuples(index=False)
    }

    # Prepare relevance first, then classify video roles in Gemini batches instead
    # of making ~100 separate Gemini calls.
    enriched_by_challenge: dict[str, list[dict[str, Any]]] = {}
    for challenge_id, group in frame.groupby("challenge_id", sort=False):
        challenge_id = str(challenge_id)
        candidate = candidate_map.get(challenge_id)
        if not candidate:
            continue
        group = group.sort_values(["views", "created_at"], ascending=[False, False]).drop_duplicates("_video_id")
        enriched: list[dict[str, Any]] = []
        for rec in group.to_dict(orient="records"):
            rec = dict(rec)
            rec["_relevance"] = _text_relevance(
                candidate["name"], candidate["aliases"],
                title=_clean_text(rec.get("title")),
                caption=_clean_text(rec.get("caption")),
                hashtags=_clean_text(rec.get("hashtags")),
                matched_alias=_clean_text(rec.get("matched_alias")),
                source_origin=_clean_text(rec.get("source_origin")),
            )
            if safe_float(rec["_relevance"]) >= fallback_min:
                enriched.append(rec)
        if enriched:
            enriched_by_challenge[challenge_id] = enriched

    ai_roles = _classify_video_roles_batched(
        candidate_map=candidate_map,
        enriched_by_challenge=enriched_by_challenge,
        config=config,
    )

    selected: list[dict[str, Any]] = []
    for challenge_id, enriched in enriched_by_challenge.items():
        if not enriched:
            continue

        strict_pool = [x for x in enriched if safe_float(x.get("_relevance")) >= min_relevance]
        working_pool = strict_pool or (enriched if fallback_enabled else [])
        if not working_pool:
            continue

        max_views = max(1.0, max(safe_float(x.get("views"), 0) for x in working_pool))
        scored: list[dict[str, Any]] = []
        for raw in working_pool:
            rec = dict(raw)
            vid = str(rec.get("_video_id", ""))
            role = ai_roles.get((challenge_id, vid), {})
            ptype = str(role.get("participation_type") or _heuristic_participation(rec))
            gtype = str(role.get("guide_type") or _heuristic_guide_type(rec, ptype))
            guide_clarity = clip01(safe_float(role.get("guide_clarity"), GUIDE_SCORES.get(gtype, 0.0)))

            if ptype == "UNRELATED" or gtype == "UNRELATED":
                continue

            views = safe_float(rec.get("views"), 0)
            likes = safe_float(rec.get("likes"), 0)
            comments = safe_float(rec.get("comments"), 0)
            popularity = math.log1p(views) / max(1e-9, math.log1p(max_views))
            engagement_rate = (likes + 2.0 * comments) / max(1.0, views)
            engagement = clip01(engagement_rate / 0.08)

            created_at = rec.get("created_at")
            if created_at is None or pd.isna(created_at):
                recency = 0.35
            else:
                age_days = max(0.0, (now_utc - pd.Timestamp(created_at)).total_seconds() / 86400.0)
                recency = math.exp(-math.log(2) * age_days / half_life)

            text = " ".join([
                _clean_text(rec.get("title")),
                _clean_text(rec.get("caption")),
                _clean_text(rec.get("hashtags")),
            ])
            inferred_kr = korean_ratio(text)
            kr_value = rec.get("kr_affinity")
            kr = inferred_kr if kr_value is None or pd.isna(kr_value) else safe_float(kr_value)
            kr = clip01(kr)
            participation = PARTICIPATION_SCORES.get(ptype, 0.25)
            guideability = max(GUIDE_SCORES.get(gtype, 0.0), guide_clarity)
            relevance = safe_float(rec.get("_relevance"))

            rep_components = {
                "relevance": relevance,
                "participation": participation,
                "popularity": popularity,
                "recency": recency,
                "engagement": engagement,
                "kr_affinity": kr,
            }
            guide_components = {
                "relevance": relevance,
                "guideability": guideability,
                "participation": participation,
                "popularity": popularity,
                "recency": recency,
                "engagement": engagement,
                "kr_affinity": kr,
            }

            rep_score = 100.0 * sum(representative_weights[k] * rep_components[k] for k in representative_weights)
            guide_score = 100.0 * sum(guide_weights[k] * guide_components[k] for k in guide_weights)
            if bool(rec.get("is_paid", False)):
                rep_score -= 100.0 * paid_penalty
                guide_score -= 50.0 * paid_penalty

            # Representative should still show the challenge itself, not a news explainer.
            if ptype == "COMMENTARY":
                rep_score -= 35.0
            # Guide should be a usable demonstration. Commentary/reaction gets a strong penalty.
            if gtype in {"COMMENTARY", "REACTION"}:
                guide_score -= 55.0
            if ptype == "REACTION":
                guide_score -= 20.0

            rec["_participation_type"] = ptype
            rec["_guide_type"] = gtype
            rec["_guide_clarity"] = guide_clarity
            rec["_representative_score"] = clip01(rep_score / 100.0) * 100.0
            rec["_guide_score"] = clip01(guide_score / 100.0) * 100.0
            scored.append(rec)

        if not scored:
            continue

        # Never re-admit COMMENTARY as the representative pick: when only
        # news/reaction coverage exists, the guide fallback below or a null
        # link is correct — a news explainer on the app card is not.
        rep_candidates = [x for x in scored if x.get("_participation_type") not in {"UNRELATED", "COMMENTARY"}]
        representative = max(
            rep_candidates,
            key=lambda x: (safe_float(x.get("_representative_score")), safe_float(x.get("views"))),
        ) if rep_candidates else None

        explicit_guides = [
            x for x in scored
            if x.get("_guide_type") in {"TUTORIAL_OR_MIRRORED", "DANCE_PRACTICE_OR_CHOREO", "CLEAR_DEMO"}
            and safe_float(x.get("_guide_score")) > 0
        ]
        guide_pool = explicit_guides or [
            x for x in scored
            if x.get("_participation_type") in {"DIRECT_PARTICIPATION", "ORIGINAL_OR_DEMO"}
            and x.get("_guide_type") not in {"COMMENTARY", "UNRELATED"}
        ]
        guide = max(
            guide_pool,
            key=lambda x: (safe_float(x.get("_guide_score")), safe_float(x.get("_guide_clarity")), safe_float(x.get("views"))),
        ) if guide_pool else None

        # Ensure both product surfaces have a usable link whenever any relevant
        # YouTube video exists. Explicit guide wins; otherwise representative is a fallback.
        if representative is None and guide is not None:
            representative = guide
        if guide is None and representative is not None:
            guide = representative

        if representative is None and guide is None:
            continue

        record: dict[str, Any] = {"challenge_id": challenge_id}
        if representative is not None:
            record.update(_representative_record(representative))
        if guide is not None:
            record.update(_guide_record(guide, fallback=(guide is representative and guide.get("_guide_type") not in {"TUTORIAL_OR_MIRRORED", "DANCE_PRACTICE_OR_CHOREO", "CLEAR_DEMO"})))
        selected.append(record)

    if not selected:
        return base[REPRESENTATIVE_COLUMNS]

    result = base[["challenge_id"]].merge(pd.DataFrame(selected), on="challenge_id", how="left")
    for col in REPRESENTATIVE_COLUMNS:
        if col == "challenge_id":
            continue
        if col in numeric_columns:
            result[col] = pd.to_numeric(result.get(col), errors="coerce").fillna(0.0)
        else:
            result[col] = result.get(col, "").fillna("").astype(str)
    return result[REPRESENTATIVE_COLUMNS]


def _representative_record(best: dict[str, Any]) -> dict[str, Any]:
    published = best.get("created_at")
    source = _clean_text(best.get("source_origin")) or "youtube_api"
    return {
        "representative_youtube_url": best.get("_youtube_url", ""),
        "representative_youtube_video_id": best.get("_video_id", ""),
        "representative_youtube_title": (_clean_text(best.get("title")) or _first_line(_clean_text(best.get("caption"))))[:200],
        "representative_youtube_channel": (_clean_text(best.get("channel_title")) or _clean_text(best.get("author_id")))[:120],
        "representative_youtube_published_at": pd.Timestamp(published).isoformat() if published is not None and not pd.isna(published) else "",
        "representative_youtube_views": int(safe_float(best.get("views"), 0)),
        "representative_youtube_score": round(safe_float(best.get("_representative_score")), 2),
        "representative_youtube_source": source,
        "representative_youtube_participation_type": str(best.get("_participation_type", "")),
    }


def _guide_record(best: dict[str, Any], *, fallback: bool) -> dict[str, Any]:
    published = best.get("created_at")
    source = _clean_text(best.get("source_origin")) or "youtube_api"
    if fallback:
        source += "_participation_fallback"
    return {
        "guide_youtube_url": best.get("_youtube_url", ""),
        "guide_youtube_video_id": best.get("_video_id", ""),
        "guide_youtube_title": (_clean_text(best.get("title")) or _first_line(_clean_text(best.get("caption"))))[:200],
        "guide_youtube_channel": (_clean_text(best.get("channel_title")) or _clean_text(best.get("author_id")))[:120],
        "guide_youtube_published_at": pd.Timestamp(published).isoformat() if published is not None and not pd.isna(published) else "",
        "guide_youtube_views": int(safe_float(best.get("views"), 0)),
        "guide_youtube_score": round(safe_float(best.get("_guide_score")), 2),
        "guide_youtube_source": source,
        "guide_youtube_type": str(best.get("_guide_type", "")),
    }


def _prepare_rows(rows: pd.DataFrame) -> pd.DataFrame:
    frame = rows.copy()
    defaults: dict[str, Any] = {
        "challenge_id": "", "platform": "", "content_id": "", "youtube_url": "", "title": "",
        "caption": "", "hashtags": "", "matched_alias": "", "author_id": "", "channel_title": "",
        "created_at": pd.NaT, "views": 0, "likes": 0, "comments": 0, "is_paid": False,
        "kr_affinity": float("nan"), "source_origin": "youtube_api",
    }
    for col, default in defaults.items():
        if col not in frame.columns:
            frame[col] = default
    frame["platform"] = frame["platform"].fillna("").astype(str).str.lower()
    explicit = frame["youtube_url"].fillna("").astype(str).str.contains(r"youtu(?:\.be|be\.com)|youtube\.com", case=False, regex=True)
    frame = frame[frame["platform"].isin(YOUTUBE_PLATFORMS) | explicit].copy()
    frame["_video_id"] = frame.apply(lambda r: extract_youtube_video_id(r.get("youtube_url")) or extract_youtube_video_id(r.get("content_id")), axis=1)
    frame = frame[frame["_video_id"].ne("")].copy()
    frame["_youtube_url"] = frame["_video_id"].map(youtube_watch_url)
    frame["created_at"] = pd.to_datetime(frame["created_at"], utc=True, errors="coerce")
    for col in ("views", "likes", "comments"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce").fillna(0).clip(lower=0)
    frame["kr_affinity"] = pd.to_numeric(frame["kr_affinity"], errors="coerce")
    frame["is_paid"] = frame["is_paid"].fillna(False).astype(bool)
    return frame


def _classify_video_roles_batched(
    *,
    candidate_map: dict[str, dict[str, Any]],
    enriched_by_challenge: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not bool(config.get("ai_participation_check", True)):
        return {}
    api_key = os.getenv(str(config.get("gemini_api_key_env", "GEMINI_API_KEY")), "").strip()
    if not api_key:
        return {}
    model = str(config.get("model", "auto"))
    max_videos = max(3, int(config.get("max_ai_videos_per_challenge", 10)))
    max_challenges = max(0, int(config.get("max_ai_challenges_per_run", 100)))
    batch_challenges = max(2, int(config.get("ai_batch_challenges", 12)))

    challenge_items: list[dict[str, Any]] = []
    ordered_ids = list(enriched_by_challenge)[:max_challenges]
    for challenge_id in ordered_ids:
        candidate = candidate_map.get(challenge_id, {})
        rows = sorted(
            enriched_by_challenge[challenge_id],
            key=lambda rec: (safe_float(rec.get("_relevance")), safe_float(rec.get("views"))),
            reverse=True,
        )[:max_videos]
        challenge_items.append({
            "challenge_id": challenge_id,
            "challenge": candidate.get("name", ""),
            "aliases": candidate.get("aliases", []),
            "videos": [
                {
                    "video_id": rec.get("_video_id", ""),
                    "title": _clean_text(rec.get("title"))[:220],
                    "description_tags": " ".join([
                        _clean_text(rec.get("caption"))[:360],
                        _clean_text(rec.get("hashtags"))[:180],
                    ]),
                    "duration_seconds": int(safe_float(rec.get("duration_seconds"), 0)),
                    "views": int(safe_float(rec.get("views"), 0)),
                }
                for rec in rows
            ],
        })

    if not challenge_items:
        return {}

    schema = {
        "type": "object",
        "properties": {
            "videos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "challenge_id": {"type": "string"},
                        "video_id": {"type": "string"},
                        "participation_type": {"type": "string", "enum": list(PARTICIPATION_SCORES)},
                        "guide_type": {"type": "string", "enum": list(GUIDE_SCORES)},
                        "guide_clarity": {"type": "number"},
                    },
                    "required": ["challenge_id", "video_id", "participation_type", "guide_type", "guide_clarity"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["videos"],
        "additionalProperties": False,
    }

    result: dict[tuple[str, str], dict[str, Any]] = {}
    for start in range(0, len(challenge_items), batch_challenges):
        batch = challenge_items[start:start + batch_challenges]
        prompt = f"""
다음은 국내 SNS 챌린지별 YouTube 후보 영상 메타데이터다. 각 영상을 두 목적에 맞게 분류하라.

participation_type:
- DIRECT_PARTICIPATION: 챌린지를 직접 수행/따라한 영상
- ORIGINAL_OR_DEMO: 원조/공식 시범/안무 시범
- REACTION: 남의 참여에 반응
- COMMENTARY: 뉴스/설명/비평/모음 중심
- UNRELATED: 다른 내용

guide_type:
- TUTORIAL_OR_MIRRORED: 튜토리얼, 거울모드, 천천히, step-by-step 등 따라하기 최적
- DANCE_PRACTICE_OR_CHOREO: 안무영상, dance practice, choreography, 연습영상
- CLEAR_DEMO: 동작/포즈/레시피/전환 과정을 비교적 명확히 보여주는 시범
- DIRECT_PARTICIPATION: 직접 참여하지만 학습용으로 특별히 구성되진 않음
- PERFORMANCE_CUT: 무대/편집/짧은 퍼포먼스로 동작 파악은 일부 가능
- REACTION / COMMENTARY / UNRELATED: 각각 의미 그대로

guide_clarity는 메타데이터상 사용자가 보고 따라 하기 쉬워 보이는 정도 0~1이다.
제목에 '안무', '튜토리얼', 'tutorial', 'dance practice', 'choreography', 'mirror/mirrored', '거울모드', 'slow', '천천히', '연습'이 있으면 강한 가이드 신호다.
영상 자체를 보지 않았으므로 메타데이터에서 확인할 수 없는 시각적 세부사항을 만들어내지 않는다.
입력된 모든 video_id를 정확히 한 번 반환한다.

데이터:
{batch}
""".strip()
        try:
            parsed = call_gemini_structured(
                api_key=api_key,
                model=model,
                system_prompt="당신은 숏폼 챌린지 영상 큐레이터다. 앱 대표영상과 따라하기 가이드영상의 목적 차이를 엄격히 구분한다.",
                user_prompt=prompt,
                schema_name="youtube_video_roles",
                schema=schema,
            )
            for item in parsed.get("videos", []):
                key = (str(item.get("challenge_id", "")), str(item.get("video_id", "")))
                if all(key):
                    result[key] = item
        except Exception:
            # Heuristics below keep the pipeline running when Gemini free-tier rate limits are hit.
            continue
    return result


def _heuristic_participation(rec: dict[str, Any]) -> str:
    text = normalize_text(" ".join([str(rec.get("title") or ""), str(rec.get("caption") or "")]))
    commentary_terms = ["이유", "정리", "뉴스", "논란", "분석", "반응 모음", "알아보기", "소개", "reaction"]
    demo_terms = ["tutorial", "튜토리얼", "안무", "dance practice", "choreography", "거울모드", "mirrored", "연습"]
    participation_terms = ["해봄", "해봤", "따라", "도전", "challenge", "챌린지", "dance", "댄스", "커버", "cover"]
    if any(t in text for t in commentary_terms):
        return "COMMENTARY"
    if any(t in text for t in demo_terms):
        return "ORIGINAL_OR_DEMO"
    if any(t in text for t in participation_terms):
        return "DIRECT_PARTICIPATION"
    return "REACTION"


def _heuristic_guide_type(rec: dict[str, Any], participation_type: str) -> str:
    text = normalize_text(" ".join([str(rec.get("title") or ""), str(rec.get("caption") or ""), str(rec.get("hashtags") or "")]))
    tutorial_terms = ["tutorial", "튜토리얼", "거울모드", "mirror", "mirrored", "slow", "천천히", "step by step", "스텝 바이 스텝", "배우기"]
    practice_terms = ["dance practice", "choreography", "안무 영상", "안무영상", "안무", "연습 영상", "연습영상", "practice", "댄스 연습"]
    clear_demo_terms = ["full dance", "full choreography", "전체 안무", "시범", "demo", "방법", "how to", "레시피", "만드는 법"]
    if any(t in text for t in tutorial_terms):
        return "TUTORIAL_OR_MIRRORED"
    if any(t in text for t in practice_terms):
        return "DANCE_PRACTICE_OR_CHOREO"
    if any(t in text for t in clear_demo_terms):
        return "CLEAR_DEMO"
    if participation_type in {"DIRECT_PARTICIPATION", "ORIGINAL_OR_DEMO"}:
        return "DIRECT_PARTICIPATION"
    if participation_type == "COMMENTARY":
        return "COMMENTARY"
    if participation_type == "UNRELATED":
        return "UNRELATED"
    return "PERFORMANCE_CUT"


def extract_youtube_video_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return ""
    if _VIDEO_ID_RE.fullmatch(text):
        return text
    if text.lower().startswith("youtube:"):
        candidate = text.split(":", 1)[1].strip()
        return candidate if _VIDEO_ID_RE.fullmatch(candidate) else ""
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
    except ValueError:
        return ""
    host = parsed.netloc.lower().split(":", 1)[0]
    parts = [x for x in parsed.path.split("/") if x]
    candidate = ""
    if host in {"youtu.be", "www.youtu.be"} and parts:
        candidate = parts[0]
    elif host.endswith("youtube.com"):
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [""])[0]
        elif parts and parts[0] in {"shorts", "embed", "live"} and len(parts) >= 2:
            candidate = parts[1]
    return candidate if _VIDEO_ID_RE.fullmatch(candidate) else ""


def youtube_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}" if _VIDEO_ID_RE.fullmatch(video_id) else ""


def _text_relevance(name: str, aliases: list[str], *, title: str, caption: str, hashtags: str, matched_alias: str, source_origin: str) -> float:
    alias_norms = [normalize_text(v) for v in [name, *aliases] if normalize_text(v)]
    title_n, caption_n, hash_n, matched_n = map(normalize_text, [title, caption, hashtags, matched_alias])
    score = 0.45 if source_origin == "observations" else 0.14
    for alias in alias_norms:
        if alias and alias in title_n:
            score = max(score, 0.96)
        if alias and alias in hash_n:
            score = max(score, 0.90)
        if alias and alias in caption_n:
            score = max(score, 0.75)
        if alias and alias == matched_n:
            score = max(score, 0.72)
    # YouTube connector searches one challenge at a time, so matched_alias is useful
    # even when the upload title uses a shortened spelling.
    if matched_n:
        score = max(score, 0.35)
    return clip01(score)


def _normalized_weights(values: dict[str, Any], defaults: dict[str, float]) -> dict[str, float]:
    weights = {k: max(0.0, safe_float(values.get(k), v)) for k, v in defaults.items()}
    total = sum(weights.values())
    return {k: v / total for k, v in weights.items()} if total > 0 else defaults.copy()


def _clean_text(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() == "nan" else " ".join(text.split())


def _first_line(value: str) -> str:
    return value.splitlines()[0].strip() if value else ""
