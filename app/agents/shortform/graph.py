from __future__ import annotations

from typing import Literal

from langgraph.graph import END, START, StateGraph

from app.agents.shortform.llm import ShortformLLM
from app.agents.shortform.types import ShortformGraphState, VideoEditingDBCandidate


def build_shortform_graph(llm: ShortformLLM):
    """Compile the Shortform Agent graph.

    Session persistence is owned by the service/DB layer. LangGraph owns the
    decision flow for each turn and recommendation selection so the LLM never
    mutates database state directly.
    """

    def route_start(state: ShortformGraphState) -> dict:
        return {}

    def choose_path(
        state: ShortformGraphState,
    ) -> Literal["decide_turn", "select_video_editing_db"]:
        return (
            "select_video_editing_db"
            if state.get("mode") == "RECOMMEND"
            else "decide_turn"
        )

    def decide_turn(state: ShortformGraphState) -> dict:
        decision = llm.decide_turn(
            domain_context=state["domain_context"],
            store_context=state["store_context"],
            project_state=state["project_state"],
            conversation=state.get("conversation", []),
            user_input=state["user_input"],
            photo_urls=state.get("photo_urls", []),
        )
        return {"decision": decision.model_dump(mode="json")}

    def select_video_editing_db(state: ShortformGraphState) -> dict:
        candidates = [
            VideoEditingDBCandidate.model_validate(item)
            for item in state["video_editing_db_candidates"]
        ]
        selection = llm.select_video_editing_db(
            domain_context=state["domain_context"],
            store_context=state["store_context"],
            project_state=state["project_state"],
            conversation=state.get("conversation", []),
            candidates=candidates,
        )
        return {"recommendation": selection.model_dump(mode="json")}

    builder = StateGraph(ShortformGraphState)
    builder.add_node("route_start", route_start)
    builder.add_node("decide_turn", decide_turn)
    builder.add_node("select_video_editing_db", select_video_editing_db)

    builder.add_edge(START, "route_start")
    builder.add_conditional_edges(
        "route_start",
        choose_path,
        {
            "decide_turn": "decide_turn",
            "select_video_editing_db": "select_video_editing_db",
        },
    )
    builder.add_edge("decide_turn", END)
    builder.add_edge("select_video_editing_db", END)
    return builder.compile()
