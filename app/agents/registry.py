from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    name: str
    status: str
    description: str
    trigger_endpoint: str
    status_endpoint_template: str
    result_endpoint_template: str
    current_result_endpoint: str
    docs_path: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


_AGENT_REGISTRY: dict[str, AgentDefinition] = {
    "challenge-ranking": AgentDefinition(
        id="challenge-ranking",
        name="Korean Challenge Ranking Agent",
        status="AVAILABLE",
        description=(
            "Discovers Instagram Reels-style challenges, validates Korean trend signals, "
            "ranks the Top 100, and selects representative and guide YouTube videos."
        ),
        trigger_endpoint="/api/v1/ranking-runs",
        status_endpoint_template="/api/v1/ranking-runs/{run_id}",
        result_endpoint_template="/api/v1/ranking-runs/{run_id}/result",
        current_result_endpoint="/api/v1/challenges?limit=100",
        docs_path="docs/BACKEND_INTEGRATION.md",
    ),
    "shortform": AgentDefinition(
        id="shortform",
        name="숏폼 Agent",
        status="AVAILABLE",
        description=(
            "Collects the project brief through conversation and recommends exactly one "
            "compatible ACTIVE video-editing template at a time."
        ),
        trigger_endpoint="/api/v1/shortform-sessions",
        status_endpoint_template="/api/v1/shortform-sessions/{session_id}/turns",
        result_endpoint_template=(
            "/api/v1/shortform-sessions/{session_id}/recommendations/next"
        ),
        current_result_endpoint="",
        docs_path="docs/BACKEND_INTEGRATION.md",
    ),
}


def list_agent_definitions() -> list[dict[str, str]]:
    return [definition.as_dict() for definition in _AGENT_REGISTRY.values()]


def get_agent_definition(agent_id: str) -> dict[str, str] | None:
    definition = _AGENT_REGISTRY.get(agent_id)
    return definition.as_dict() if definition else None
