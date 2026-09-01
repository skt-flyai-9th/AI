from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from dotenv import load_dotenv

from .aggregation import aggregate_content_rows
from .ai_analysis import apply_ai_adjudication
from .auto_discovery import discover_live_challenges
from .config import load_config
from .connectors.base import ConnectorResult
from .connectors.apify_instagram import InstagramApifyConnector
from .connectors.naver import NaverBlogConnector, NaverDatalabConnector, NaverNewsConnector
from .connectors.x_api import XCountsConnector
from .connectors.youtube import YouTubeConnector
from .db import load_previous_ranking, save_run
from .features import build_features
from .io import load_candidates, load_observations, write_csv_atomic, write_json_atomic
from .representative import select_representative_youtube
from .scoring import score_challenges
from .utils import parse_now, redact_sensitive_data


@dataclass
class RunResult:
    run_id: str
    run_at: pd.Timestamp
    ranking: pd.DataFrame
    features: pd.DataFrame
    source_metrics: pd.DataFrame
    statuses: dict[str, dict[str, Any]]
    output_paths: dict[str, str]


def run_from_config(config_path: str | Path) -> RunResult:
    config, resolved_config_path = load_config(config_path)
    load_dotenv(resolved_config_path.parent / ".env", override=False)
    load_dotenv(override=False)
    return run_pipeline(config)


def run_pipeline(config: dict[str, Any]) -> RunResult:
    timezone_name = str(config.get("timezone", "Asia/Seoul"))
    now = parse_now(config.get("now"), timezone_name)
    statuses: dict[str, dict[str, Any]] = {}

    auto_cfg = config.get("auto_discovery", {})
    if auto_cfg.get("enabled", False):
        discovery_result = discover_live_challenges(
            auto_cfg, now, output_path=config["paths"]["candidates_csv"]
        )
        candidates = discovery_result.candidates
        statuses["auto_discovery"] = discovery_result.status
    else:
        candidates = load_candidates(config["paths"]["candidates_csv"], timezone_name)

    excluded_ids = {
        str(value).strip()
        for value in config.get("ranking", {}).get("exclude_challenge_ids", [])
        if str(value).strip()
    }
    if excluded_ids and not candidates.empty:
        candidates = candidates[
            ~candidates["challenge_id"].fillna("").astype(str).isin(excluded_ids)
        ].reset_index(drop=True)
    if candidates.empty:
        raise RuntimeError("트렌드 리서치 후보가 없습니다.")

    metric_frames: list[pd.DataFrame] = []
    representative_row_frames: list[pd.DataFrame] = []

    observations_cfg = config.get("sources", {}).get("observations", {})
    if observations_cfg.get("enabled", True):
        observations = load_observations(
            config["paths"]["observations_csv"], candidates, timezone_name
        )
        local_metrics = aggregate_content_rows(candidates, observations, now, prefix="local")
        metric_frames.append(local_metrics)
        statuses["observations"] = {
            "enabled": True,
            "success": True,
            "rows": int(len(observations)),
            "path": config["paths"]["observations_csv"],
        }
        if not observations.empty:
            local_youtube = observations[
                observations["platform"].fillna("").astype(str).str.lower().str.contains(
                    r"youtube|shorts", regex=True
                )
            ].copy()
            if not local_youtube.empty:
                local_youtube["source_origin"] = "observations"
                local_youtube["title"] = local_youtube["caption"]
                local_youtube["channel_title"] = local_youtube["author_id"]
                local_youtube["youtube_url"] = ""
                local_youtube["matched_alias"] = ""
                representative_row_frames.append(local_youtube)
    else:
        statuses["observations"] = {"enabled": False, "success": False, "skipped": True}

    # --------------------------------------------------------
    # 1) Instagram + NAVER first.
    #    We use those signals to choose the ranked candidate pool worth spending
    #    YouTube search.list calls on. Reserve candidates allow URL failures to
    #    be replaced before publishing the complete Top 100.
    # --------------------------------------------------------
    non_youtube_specs = [
        ("instagram_apify", InstagramApifyConnector, (timezone_name,)),
        ("naver_datalab", NaverDatalabConnector, (timezone_name,)),
        ("naver_blog", NaverBlogConnector, (timezone_name,)),
        ("naver_news", NaverNewsConnector, ()),
        ("x", XCountsConnector, ()),
    ]
    for source_name, connector_class, extra_args in non_youtube_specs:
        source_cfg = config.get("sources", {}).get(source_name, {})
        if not source_cfg.get("enabled", False):
            statuses[source_name] = {"enabled": False, "success": False, "skipped": True}
            continue
        connector = connector_class(source_cfg, *extra_args)
        result: ConnectorResult = connector.collect(candidates, now)
        statuses[source_name] = result.status
        if not result.metrics.empty:
            metric_frames.append(result.metrics)

    preliminary_metrics = _merge_metric_frames(candidates, metric_frames)
    preliminary_features = build_features(candidates, preliminary_metrics, statuses, now)
    preliminary_ranking = score_challenges(preliminary_features, config["ranking"])

    # --------------------------------------------------------
    # 2) YouTube search.list over the provisional Top 100 plus a reserve pool.
    #    One successful result set feeds BOTH representative and guide ranking.
    # --------------------------------------------------------
    youtube_cfg = config.get("sources", {}).get("youtube", {})
    if youtube_cfg.get("enabled", False):
        target_count = max(1, int(config.get("ranking", {}).get("top_n", 100)))
        max_youtube_challenges = min(
            len(candidates),
            max(target_count, int(youtube_cfg.get("max_challenges", target_count))),
            max(1, int(youtube_cfg.get("max_search_requests", target_count))),
        )
        provisional = preliminary_ranking.sort_values(
            ["final_rank", "confidence"], ascending=[True, False]
        )[["challenge_id"]].head(max_youtube_challenges)
        youtube_candidates = provisional.merge(candidates, on="challenge_id", how="left")
        connector = YouTubeConnector(youtube_cfg)
        yt_result: ConnectorResult = connector.collect(youtube_candidates, now)
        statuses["youtube"] = yt_result.status
        if not yt_result.metrics.empty:
            metric_frames.append(yt_result.metrics)
        if yt_result.raw_rows is not None and not yt_result.raw_rows.empty:
            representative_row_frames.append(yt_result.raw_rows.copy())
    else:
        statuses["youtube"] = {"enabled": False, "success": False, "skipped": True}

    # Final metrics/rank include YouTube cross-platform evidence when available.
    source_metrics = _merge_metric_frames(candidates, metric_frames)
    features = build_features(candidates, source_metrics, statuses, now)
    ranking = score_challenges(features, config["ranking"])

    representative_rows = (
        pd.concat(representative_row_frames, ignore_index=True, sort=False)
        if representative_row_frames
        else pd.DataFrame()
    )
    representatives = select_representative_youtube(
        candidates,
        representative_rows,
        now,
        config.get("representative_youtube", {}),
    )
    ranking = ranking.merge(representatives, on="challenge_id", how="left")
    _fill_representative_columns(ranking)

    ai_cfg = config.get("ai_adjudication", {})
    if ai_cfg.get("enabled", False):
        ranking, ai_status = apply_ai_adjudication(
            ranking, candidates, source_metrics, ai_cfg
        )
        statuses["ai_adjudication"] = ai_status
    else:
        statuses["ai_adjudication"] = {
            "enabled": False, "success": False, "skipped": True
        }

    previous = load_previous_ranking(config["paths"]["database"])
    ranking = ranking.merge(previous, on="challenge_id", how="left")
    ranking["rank_change"] = ranking["previous_rank"] - ranking["final_rank"]
    ranking["score_change"] = ranking["final_score"] - ranking["previous_score"]
    ranking = ranking.drop(columns=["previous_rank", "previous_score"], errors="ignore")

    run_id = f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    statuses = redact_sensitive_data(statuses)
    output_paths = _write_outputs(config, run_id, now, ranking, features, source_metrics, statuses)
    save_run(
        config["paths"]["database"],
        run_id=run_id,
        run_at=now,
        statuses=statuses,
        config=config,
        ranking=ranking,
        features=features,
        source_metrics=source_metrics,
    )

    return RunResult(
        run_id=run_id,
        run_at=now,
        ranking=ranking,
        features=features,
        source_metrics=source_metrics,
        statuses=statuses,
        output_paths=output_paths,
    )

def _merge_metric_frames(candidates: pd.DataFrame, frames: list[pd.DataFrame]) -> pd.DataFrame:
    merged = candidates[["challenge_id"]].copy()
    for frame in frames:
        if frame.empty or "challenge_id" not in frame.columns:
            continue
        deduped = frame.drop_duplicates(subset=["challenge_id"], keep="last")
        overlapping = [
            column
            for column in deduped.columns
            if column != "challenge_id" and column in merged.columns
        ]
        if overlapping:
            deduped = deduped.drop(columns=overlapping)
        merged = merged.merge(deduped, on="challenge_id", how="left")
    numeric_columns = [column for column in merged.columns if column != "challenge_id"]
    for column in numeric_columns:
        if pd.api.types.is_numeric_dtype(merged[column]) or merged[column].isna().all():
            merged[column] = pd.to_numeric(merged[column], errors="coerce").fillna(0.0)
    return merged


def _fill_representative_columns(ranking: pd.DataFrame) -> None:
    string_columns = [
        "representative_youtube_url",
        "representative_youtube_video_id",
        "representative_youtube_title",
        "representative_youtube_channel",
        "representative_youtube_published_at",
        "representative_youtube_source",
        "representative_youtube_participation_type",
        "guide_youtube_url",
        "guide_youtube_video_id",
        "guide_youtube_title",
        "guide_youtube_channel",
        "guide_youtube_published_at",
        "guide_youtube_source",
        "guide_youtube_type",
    ]
    numeric_columns = [
        "representative_youtube_views",
        "representative_youtube_score",
        "guide_youtube_views",
        "guide_youtube_score",
    ]
    for column in string_columns:
        if column not in ranking.columns:
            ranking[column] = ""
        ranking[column] = ranking[column].fillna("").astype(str)
    for column in numeric_columns:
        if column not in ranking.columns:
            ranking[column] = 0.0
        ranking[column] = pd.to_numeric(ranking[column], errors="coerce").fillna(0.0)

def _build_public_ranking(config: dict[str, Any], ranking: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    ranked = ranking.sort_values(["final_rank", "confidence"], ascending=[True, False]).copy()
    for column in ("representative_youtube_url", "guide_youtube_url"):
        if column not in ranked.columns:
            ranked[column] = ""
        ranked[column] = ranked[column].fillna("").astype(str)

    require_video = bool(config.get("ranking", {}).get("require_youtube_video", False))
    if require_video:
        ranked = ranked[
            ranked["representative_youtube_url"].ne("")
            & ranked["guide_youtube_url"].ne("")
        ].copy()

    top_n = max(1, int(config.get("ranking", {}).get("top_n", 100)))
    ranking_cfg = config.get("ranking", {})
    exclude_ai = bool(ranking_cfg.get("exclude_ai_rejected", True))
    backfill = bool(ranking_cfg.get("backfill_to_top_n", True))

    if exclude_ai and "is_social_challenge" in ranked.columns:
        social_mask = ranked["is_social_challenge"].fillna(True).astype(bool)
        accepted = ranked[social_mask].copy()
        if backfill and len(accepted) < top_n:
            rejected = ranked[~social_mask].copy()
            # Backfill only source-backed candidates. Clear false positives still
            # receive the AI 0.1 score multiplier, so they appear only at the tail.
            if "instagram_posts_7d" in rejected.columns:
                rejected = rejected[pd.to_numeric(rejected["instagram_posts_7d"], errors="coerce").fillna(0) > 0]
            elif "posts_7d" in rejected.columns:
                rejected = rejected[pd.to_numeric(rejected["posts_7d"], errors="coerce").fillna(0) > 0]
            if "entity_confidence" in rejected.columns:
                rejected = rejected[pd.to_numeric(rejected["entity_confidence"], errors="coerce").fillna(0) >= float(ranking_cfg.get("backfill_min_entity_confidence", 0.15))]
            needed = max(0, top_n - len(accepted))
            # Backfilled rejected rows go strictly after every accepted row; a
            # combined score re-sort would let a penalized viral false positive
            # climb back above weaker genuine challenges.
            ranked = pd.concat([accepted, rejected.head(needed)], ignore_index=False)
        else:
            ranked = accepted

    ranked = ranked.head(top_n).reset_index(drop=True)
    ranked["published_rank"] = range(1, len(ranked) + 1)

    public = pd.DataFrame(
        {
            "id": ranked["challenge_id"],
            "rank": ranked["published_rank"],
            "name": ranked["name"],
            "representative_youtube_url": ranked["representative_youtube_url"],
            "guide_youtube_url": ranked["guide_youtube_url"],
        }
    )
    return public, ranked

def _write_outputs(
    config: dict[str, Any],
    run_id: str,
    now: pd.Timestamp,
    ranking: pd.DataFrame,
    features: pd.DataFrame,
    source_metrics: pd.DataFrame,
    statuses: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Write JSON-only local exports.

    PostgreSQL is the application source of truth. These files are operational
    backups/debugging artifacts and intentionally exclude HTML/CSV rendering.
    """
    output_dir = Path(config["paths"]["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    public_export, published_details = _build_public_ranking(config, ranking)

    detail_columns = [
        "published_rank", "final_rank", "rank_change", "challenge_id", "name",
        "representative_youtube_url", "representative_youtube_video_id",
        "representative_youtube_title", "representative_youtube_channel",
        "representative_youtube_published_at", "representative_youtube_views",
        "representative_youtube_score", "representative_youtube_source",
        "representative_youtube_participation_type", "guide_youtube_url",
        "guide_youtube_video_id", "guide_youtube_title", "guide_youtube_channel",
        "guide_youtube_published_at", "guide_youtube_views", "guide_youtube_score",
        "guide_youtube_source", "guide_youtube_type", "category", "stage",
        "recommended_action", "final_score", "emerging_rank", "emerging_score",
        "mainstream_rank", "mainstream_score", "saturation_score", "confidence",
        "participation_acceleration", "instagram_audio_reuse_ratio",
        "unique_creators_7d", "posts_7d", "views_7d", "cross_platform_count",
        "search_lift", "search_acceleration", "blog_7d", "blog_growth",
        "organic_breadth", "kr_affinity", "age_days", "score_change",
        "data_final_score", "is_social_challenge", "trend_score",
        "domestic_relevance", "evidence_quality", "ai_reason",
    ]
    detail_columns = [c for c in detail_columns if c in published_details.columns]
    details_export = published_details[detail_columns].copy()

    paths = {
        "trendcluster_json": str(output_dir / "trendcluster.json"),
        "ranking_details_latest_json": str(output_dir / "ranking_details_latest.json"),
        "ranking_full_latest_json": str(output_dir / "ranking_full_latest.json"),
        "source_metrics_latest_json": str(output_dir / "source_metrics_latest.json"),
        "status_latest_json": str(output_dir / "run_status_latest.json"),
    }
    trendcluster_payload = {
        "generated_at": now.isoformat(),
        "count": int(len(public_export)),
        "results": public_export.to_dict(orient="records"),
    }
    write_json_atomic(trendcluster_payload, paths["trendcluster_json"])
    write_json_atomic(details_export.to_dict(orient="records"), paths["ranking_details_latest_json"])
    write_json_atomic(ranking.sort_values("final_rank").to_dict(orient="records"), paths["ranking_full_latest_json"])
    write_json_atomic(source_metrics.to_dict(orient="records"), paths["source_metrics_latest_json"])

    status_payload = {
        "run_id": run_id,
        "run_at": now.isoformat(),
        "requested_top_n": int(config.get("ranking", {}).get("top_n", 100)),
        "discovered_ranked_challenges": int(len(ranking)),
        "published_challenges": int(len(public_export)),
        "published_with_representative_video": int(
            public_export["representative_youtube_url"].fillna("").astype(str).ne("").sum()
        ),
        "published_with_guide_video": int(
            public_export["guide_youtube_url"].fillna("").astype(str).ne("").sum()
        ),
        "statuses": statuses,
        "output_paths": paths,
    }
    Path(paths["status_latest_json"]).write_text(
        json.dumps(status_payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return paths
