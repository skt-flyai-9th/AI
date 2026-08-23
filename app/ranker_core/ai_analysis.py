from __future__ import annotations

import os
from typing import Any

import pandas as pd

from .gemini_json import call_gemini_structured


def apply_ai_adjudication(
    ranking: pd.DataFrame,
    candidates: pd.DataFrame,
    source_metrics: pd.DataFrame,
    config: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Use AI as a final semantic filter and bounded score adjustment.

    The quantitative rank remains the primary signal. AI can remove obvious false
    positives and contribute a configurable fraction of the final trend score.
    """
    if ranking.empty:
        return ranking, {"enabled": True, "success": True, "rows": 0}

    key_env = str(config.get("gemini_api_key_env", "GEMINI_API_KEY"))
    api_key = os.getenv(key_env, "").strip()
    if not api_key:
        return ranking, {
            "enabled": True,
            "success": False,
            "skipped": True,
            "reason": f"환경변수 {key_env}가 없습니다.",
        }

    model = str(config.get("model", os.getenv("GEMINI_MODEL", "auto")))
    top_for_ai = max(3, int(config.get("max_candidates", 20)))
    ai_weight = min(0.5, max(0.0, float(config.get("weight", 0.25))))

    merged = ranking.merge(
        candidates[["challenge_id", "aliases", "entity_confidence", "kr_affinity_hint"]],
        on="challenge_id",
        how="left",
        suffixes=("", "_candidate"),
    )
    if not source_metrics.empty:
        extra_cols = [
            c
            for c in source_metrics.columns
            if c == "challenge_id"
            or c.startswith("youtube_")
            or c.startswith("naver_")
        ]
        merged = merged.merge(source_metrics[extra_cols], on="challenge_id", how="left")

    merged = merged.sort_values(["final_score", "confidence"], ascending=[False, False]).head(top_for_ai)
    evidence_rows = []
    for row in merged.to_dict(orient="records"):
        evidence_rows.append(
            {
                "challenge_id": row.get("challenge_id"),
                "name": row.get("name"),
                "aliases": row.get("aliases", ""),
                "category": row.get("category", ""),
                "data_score": _round(row.get("final_score")),
                "confidence": _round(row.get("confidence")),
                "kr_affinity": _round(row.get("kr_affinity")),
                "youtube_creators_7d": _round(row.get("youtube_creators_7d")),
                "youtube_posts_7d": _round(row.get("youtube_posts_7d")),
                "youtube_views_7d": _round(row.get("youtube_views_7d")),
                "youtube_creator_growth_24h": _round(row.get("youtube_creator_growth_24h")),
                "naver_search_lift_3d": _round(row.get("naver_search_lift_3d")),
                "naver_search_acceleration": _round(row.get("naver_search_acceleration")),
                "naver_blog_7d": _round(row.get("naver_blog_7d")),
                "naver_blog_growth_7d": _round(row.get("naver_blog_growth_7d")),
                "naver_news_7d": _round(row.get("naver_news_7d")),
                "representative_youtube_title": row.get("representative_youtube_title", ""),
                "representative_youtube_channel": row.get("representative_youtube_channel", ""),
                "representative_youtube_views": _round(row.get("representative_youtube_views")),
            }
        )

    # Top 100에서는 한 번의 거대한 Structured Output보다 작은 배치가 안정적입니다.
    batch_size = max(10, int(config.get("batch_size", 35)))
    parsed_judgements: list[dict[str, Any]] = []
    batch_errors: list[str] = []
    for start in range(0, len(evidence_rows), batch_size):
        batch = evidence_rows[start : start + batch_size]
        prompt = f"""
다음은 자동 탐지된 국내 챌린지 후보의 API 검증 지표다.
각 후보가 실제 SNS 참여형 챌린지인지 최종 판정하고, 한국에서 현재 유행하는 정도를 0~100으로 평가하라.

정의:
- 참여형 챌린지: 여러 독립 이용자가 동일한 행동/댄스/포즈/밈 템플릿/레시피/변신 포맷 등을 재현·변주하는 현상.
- 명백한 제외: 게임 도전과제, TV/예능 제목 자체, 기업 단독 이벤트, 스포츠 경기, 일반적인 자기계발 목표.

규칙:
- is_social_challenge=false는 위처럼 명백히 참여형 SNS 챌린지가 아닌 경우에만 사용한다.
- 증거가 약하거나 이름이 아직 정착되지 않은 Instagram형 초기 챌린지는 false로 지우지 말고 true로 유지하되 trend_score/evidence_quality를 낮게 준다.
- 제공된 숫자와 대표 영상 제목만 사용한다. 외부 최신 사실을 추측하지 않는다.
- trend_score는 현재 국내 확산력/증가속도/검색 파급을 종합한 0~100.
- domestic_relevance는 한국 내 유행 근거 0~1.
- evidence_quality는 데이터가 이 판정을 뒷받침하는 정도 0~1.
- 데이터가 약하면 낮게 평가한다.
- challenge_id는 입력 값을 그대로 반환한다.
- 입력된 모든 후보에 대해 정확히 한 개의 judgement를 반환한다.

후보 데이터:
{batch}
""".strip()
        try:
            parsed = call_gemini_structured(
                api_key=api_key,
                model=model,
                system_prompt="당신은 한국 숏폼/SNS 유행을 데이터로 검증하는 분석가다. 의미 판정은 보수적으로 하고 수치 증거를 우선한다.",
                user_prompt=prompt,
                schema_name="challenge_judgement",
                schema=_judgement_schema(),
            )
            parsed_judgements.extend(parsed.get("judgements", []))
        except Exception as exc:
            batch_errors.append(str(exc))

    if not parsed_judgements:
        return ranking, {
            "enabled": True,
            "success": False,
            "skipped": False,
            "error": batch_errors[-1] if batch_errors else "Gemini judgement 결과 없음",
            "model": model,
        }

    judgements = pd.DataFrame(parsed_judgements)
    if judgements.empty:
        return ranking, {"enabled": True, "success": True, "rows": 0, "model": model}

    result = ranking.merge(judgements, on="challenge_id", how="left")
    result["is_social_challenge"] = result["is_social_challenge"].fillna(True).astype(bool)
    result["trend_score"] = pd.to_numeric(result["trend_score"], errors="coerce")
    result["domestic_relevance"] = pd.to_numeric(result["domestic_relevance"], errors="coerce")
    result["evidence_quality"] = pd.to_numeric(result["evidence_quality"], errors="coerce")
    result["trend_score"] = result["trend_score"].fillna(result["final_score"]).clip(0, 100)
    result["domestic_relevance"] = result["domestic_relevance"].fillna(result["kr_affinity"]).clip(0, 1)
    result["evidence_quality"] = result["evidence_quality"].fillna(0.5).clip(0, 1)
    result["ai_reason"] = result["reason"].fillna("").astype(str)
    result = result.drop(columns=["reason"], errors="ignore")

    data_score = result["final_score"].astype(float)
    ai_score = result["trend_score"].astype(float)
    domestic_guard = 0.70 + 0.30 * result["domestic_relevance"]
    evidence_guard = 0.85 + 0.15 * result["evidence_quality"]
    result["data_final_score"] = data_score
    result["final_score"] = (
        ((1.0 - ai_weight) * data_score + ai_weight * ai_score)
        * domestic_guard
        * evidence_guard
    ).clip(0, 100)
    # Obvious semantic false positives remain in detail output but cannot win public ranking.
    result.loc[~result["is_social_challenge"], "final_score"] *= 0.10
    result["final_rank"] = result["final_score"].rank(method="min", ascending=False).astype(int)
    result = result.sort_values(["final_rank", "confidence"], ascending=[True, False]).reset_index(drop=True)

    return result, {
        "enabled": True,
        "success": True,
        "rows": int(len(judgements)),
        "model": model,
        "weight": ai_weight,
        "rejected": int((~result["is_social_challenge"]).sum()),
        "batches": int((len(evidence_rows) + batch_size - 1) // batch_size),
        "batch_errors": batch_errors,
    }


def _judgement_schema() -> dict[str, Any]:
    item = {
        "type": "object",
        "properties": {
            "challenge_id": {"type": "string"},
            "is_social_challenge": {"type": "boolean"},
            "trend_score": {"type": "number"},
            "domestic_relevance": {"type": "number"},
            "evidence_quality": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": [
            "challenge_id", "is_social_challenge", "trend_score",
            "domestic_relevance", "evidence_quality", "reason"
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"judgements": {"type": "array", "items": item}},
        "required": ["judgements"],
        "additionalProperties": False,
    }


def _round(value: Any) -> float:
    try:
        if value is None or pd.isna(value):
            return 0.0
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0
