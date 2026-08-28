from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph

from app.agents.editing.llm import EditingLLM
from app.agents.editing.recipe_normalizer import normalize_recipe_for_rendering
from app.agents.editing.types import EditingGraphState, EditingPlanDecision, VideoContext
from app.agents.editing.validator import EditRecipeValidator
from app.schemas.editing import EditRecipe, SelectedShortform


def build_editing_graph(llm: EditingLLM, validator: EditRecipeValidator):
    """Compile plan -> deterministic validation -> bounded repair loop."""

    def plan_recipe(state: EditingGraphState) -> dict:
        _emit_stage(state, "PLANNING_RECIPE", 35)

        def update_analysis_progress(progress: int) -> None:
            _emit_stage(state, "PLANNING_RECIPE", progress)

        decision = llm.plan_recipe(
            domain_context=state["domain_context"],
            project=state["project"],
            selected_shortform=state["selected_shortform"],
            video_editing_db=state["video_editing_db"],
            video_contexts=_contexts(state),
            parent_recipe=state.get("parent_recipe"),
            revision_action=state.get("revision_action"),
            progress_callback=update_analysis_progress,
            checkpoint_callback=state.get("checkpoint_callback"),
        )
        return {"decision": decision.model_dump(mode="json"), "repair_attempts": 0}

    def validate_recipe(state: EditingGraphState) -> dict:
        _emit_stage(state, "VALIDATING_RECIPE", 65)
        decision = EditingPlanDecision.model_validate(state["decision"])
        if decision.outcome == "SOURCE_GAP":
            return {"validation_errors": [], "validation_passed": True}
        normalized_recipe = normalize_recipe_for_rendering(
            EditRecipe.model_validate(decision.recipe)
        )
        decision = decision.model_copy(update={"recipe": normalized_recipe})
        errors = validator.validate(
            normalized_recipe,
            selected_shortform=SelectedShortform.model_validate(state["selected_shortform"]),
            video_editing_db=state["video_editing_db"],
            video_contexts=_contexts(state),
            project=state["project"],
        )
        return {
            "decision": decision.model_dump(mode="json"),
            "validation_errors": [error.model_dump(mode="json") for error in errors],
            "validation_passed": not errors,
        }

    def route_validation(state: EditingGraphState) -> Literal["done", "repair", "exhausted"]:
        if state.get("validation_passed"):
            return "done"
        if state.get("repair_attempts", 0) < state.get("max_repair_attempts", 2):
            return "repair"
        return "exhausted"

    def repair_recipe(state: EditingGraphState) -> dict:
        _emit_stage(state, "PLANNING_RECIPE", 65)
        decision = llm.repair_recipe(
            domain_context=state["domain_context"],
            project=state["project"],
            selected_shortform=state["selected_shortform"],
            video_editing_db=state["video_editing_db"],
            video_contexts=_contexts(state),
            decision=EditingPlanDecision.model_validate(state["decision"]),
            validation_errors=state["validation_errors"],
            parent_recipe=state.get("parent_recipe"),
            revision_action=state.get("revision_action"),
            progress_callback=lambda progress: _emit_stage(
                state, "PLANNING_RECIPE", max(65, progress)
            ),
            checkpoint_callback=state.get("checkpoint_callback"),
        )
        return {
            "decision": decision.model_dump(mode="json"),
            "repair_attempts": state.get("repair_attempts", 0) + 1,
        }

    def mark_exhausted(_: EditingGraphState) -> dict:
        return {"exhausted": True}

    builder = StateGraph(EditingGraphState)
    builder.add_node("plan_recipe", plan_recipe)
    builder.add_node("validate_recipe", validate_recipe)
    builder.add_node("repair_recipe", repair_recipe)
    builder.add_node("mark_exhausted", mark_exhausted)
    builder.add_edge(START, "plan_recipe")
    builder.add_edge("plan_recipe", "validate_recipe")
    builder.add_conditional_edges(
        "validate_recipe",
        route_validation,
        {"done": END, "repair": "repair_recipe", "exhausted": "mark_exhausted"},
    )
    builder.add_edge("repair_recipe", "validate_recipe")
    builder.add_edge("mark_exhausted", END)
    return builder.compile()


def _contexts(state: EditingGraphState) -> list[VideoContext]:
    return [VideoContext.model_validate(item) for item in state["video_contexts"]]


def _emit_stage(state: EditingGraphState, stage: str, progress: int) -> None:
    callback = state.get("stage_callback")
    if callback is not None:
        callback(stage, progress)
