from __future__ import annotations

from typing import Any

from app.agents.harness import AgentHarness, HarnessContract
from app.agents.shortform.types import ShortformTurnDecision, VideoEditingDBSelections


def _validate_turn(_: Any, output_value: Any) -> tuple[str, ...]:
    try:
        ShortformTurnDecision.model_validate(output_value["decision"])
    except (KeyError, TypeError, ValueError):
        return ("SHORTFORM_TURN_DECISION_INVALID",)
    return ()


def _validate_recommendation(input_value: Any, output_value: Any) -> tuple[str, ...]:
    try:
        selections = VideoEditingDBSelections.model_validate(
            output_value["recommendations"]
        ).selections
    except (KeyError, TypeError, ValueError):
        return ("SHORTFORM_RECOMMENDATIONS_INVALID",)

    candidate_keys = {
        str(item.get("candidate_key") or "")
        for item in input_value["video_editing_db_candidates"]
    }
    if any(selection.candidate_key not in candidate_keys for selection in selections):
        return ("SHORTFORM_SELECTION_OUTSIDE_CANDIDATE_POOL",)
    return ()


shortform_harness = AgentHarness(
    agent_id="shortform",
    contracts={
        "turn": HarnessContract(
            required_inputs=(
                "mode",
                "domain_context",
                "store_context",
                "project_state",
                "conversation",
                "user_input",
                "photo_urls",
            ),
            required_outputs=("decision",),
            validator=_validate_turn,
            max_validation_attempts=2,
        ),
        "recommend": HarnessContract(
            required_inputs=(
                "mode",
                "domain_context",
                "store_context",
                "project_state",
                "conversation",
                "video_editing_db_candidates",
            ),
            required_outputs=("recommendations",),
            validator=_validate_recommendation,
            max_validation_attempts=2,
        ),
    },
)
