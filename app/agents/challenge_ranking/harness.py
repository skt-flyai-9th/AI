from __future__ import annotations

from typing import Any

from app.agents.harness import AgentHarness, HarnessContract
from app.ranker_core.representative import extract_youtube_video_id


def _validate_research_result(input_value: Any, output_value: Any) -> tuple[str, ...]:
    ranking_config = input_value.get("ranking", {})
    target_count = max(1, int(ranking_config.get("top_n", 100)))
    require_youtube = bool(ranking_config.get("require_youtube_video", False))
    excluded_ids = {
        str(value).strip()
        for value in ranking_config.get("exclude_challenge_ids", [])
        if str(value).strip()
    }
    ranking = output_value.ranking
    if ranking is None or ranking.empty:
        return ("RANKING_EMPTY",)

    valid_ids: set[str] = set()
    for row in ranking.to_dict(orient="records"):
        challenge_id = str(row.get("challenge_id") or "").strip()
        if not challenge_id or challenge_id in excluded_ids or challenge_id in valid_ids:
            continue
        if "is_social_challenge" in row and not bool(row.get("is_social_challenge")):
            continue
        if require_youtube:
            representative_id = extract_youtube_video_id(row.get("representative_youtube_url"))
            guide_id = extract_youtube_video_id(row.get("guide_youtube_url"))
            if not representative_id or not guide_id:
                continue
        valid_ids.add(challenge_id)
        if len(valid_ids) >= target_count:
            return ()
    return ("INSUFFICIENT_VALID_RANKED_TRENDS",)


challenge_ranking_harness = AgentHarness(
    agent_id="challenge-ranking",
    contracts={
        "research": HarnessContract(
            required_inputs=("paths", "ranking", "sources"),
            required_outputs=("run_id", "ranking", "source_metrics", "statuses"),
            validator=_validate_research_result,
        )
    },
)
