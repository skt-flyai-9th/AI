from __future__ import annotations

from typing import Any

from app.agents.harness import AgentHarness, HarnessContract
from app.agents.editing.types import EditingPlanDecision


_EDITING_INPUTS = (
    "domain_context",
    "project",
    "selected_shortform",
    "video_editing_db",
    "videos",
    "video_contexts",
    "max_repair_attempts",
    "repair_attempts",
    "stage_callback",
    "checkpoint_callback",
)


def _validate_editing_result(_: Any, output_value: Any) -> tuple[str, ...]:
    if bool(output_value.get("exhausted")):
        return ()
    if output_value.get("validation_passed") is not True:
        return ("EDITING_RECIPE_NOT_VALIDATED",)
    try:
        EditingPlanDecision.model_validate(output_value["decision"])
    except (KeyError, TypeError, ValueError):
        return ("EDITING_PLAN_DECISION_INVALID",)
    return ()

editing_harness = AgentHarness(
    agent_id="editing",
    contracts={
        "plan": HarnessContract(
            required_inputs=_EDITING_INPUTS,
            required_outputs=("decision", "validation_passed"),
            validator=_validate_editing_result,
        ),
        "reduced_plan": HarnessContract(
            required_inputs=_EDITING_INPUTS,
            required_outputs=("decision", "validation_passed"),
            validator=_validate_editing_result,
        ),
    },
)
