from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.shortform import (
    FaceExposure,
    FilmingTime,
    PromotionObjective,
    PromotionSubject,
    ShortformAction,
    ShortformOption,
)


class StateUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion_category: str | None = None
    promotion_subject: PromotionSubject | None = None
    promotion_objective: PromotionObjective | None = None
    filming_time: FilmingTime | None = None
    face_exposure: FaceExposure | None = None
    creative_preferences: list[str] = Field(default_factory=list)
    secondary_information: list[str] = Field(default_factory=list)
    facts_from_user: dict[str, str] = Field(default_factory=dict)


class ConflictItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    stored_value: str
    user_value: str
    message: str


class ShortformTurnDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: ShortformAction
    assistant_message: str
    state_updates: StateUpdates = Field(default_factory=StateUpdates)
    options: list[ShortformOption] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    conflicts: list[ConflictItem] = Field(default_factory=list)
    ready_for_confirmation: bool = False


class TemplateCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_key: str
    editing_template_id: str
    editing_template_version: int
    name: str
    recommendation_title: str
    recommendation_concept: str
    recommendation_metadata: dict[str, Any] = Field(default_factory=dict)
    trend_context: list[dict[str, Any]] = Field(default_factory=list)


class TemplateSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_key: str
    project_title: str
    title: str
    concept: str
    internal_reason: str


class ShortformGraphState(dict):
    """Marker type for runtime dict state.

    LangGraph accepts mapping-like state. The public API state remains Pydantic
    validated at the service boundary; this marker keeps graph wiring lightweight.
    """


GraphMode = Literal["TURN", "RECOMMEND"]
