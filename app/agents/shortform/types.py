from __future__ import annotations

from typing import Any, Literal
from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.shortform import (
    FaceExposure,
    FilmingTime,
    PromotionCategory,
    PromotionObjective,
    ShortformAction,
)


class FactItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    value: str


class DecisionPromotionSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str | None
    name: str | None
    menu_id: str | None
    details: list[FactItem]


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    label: str


class StateUpdates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion_category: PromotionCategory | None
    promotion_subject: DecisionPromotionSubject | None
    promotion_objective: PromotionObjective | None
    filming_time: FilmingTime | None
    face_exposure: FaceExposure | None
    creative_preferences: list[str]
    secondary_information: list[str]
    facts_from_user: list[FactItem]


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
    state_updates: StateUpdates
    options: list[DecisionOption]
    missing_required_fields: list[str]
    conflicts: list[ConflictItem]
    ready_for_confirmation: bool


class VideoEditingDBCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_key: str
    video_editing_db_id: str
    video_editing_db_version: int
    name: str
    recommendation_title: str
    recommendation_concept: str
    recommendation_metadata: dict[str, Any] = Field(default_factory=dict)
    trend_context: list[dict[str, Any]] = Field(default_factory=list)
    reference_url: str
    guide_video_url: str
    source_platform: str = "YOUTUBE"


class VideoEditingDBSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_key: str
    project_title: str
    title: str
    concept: str
    internal_reason: str


class VideoEditingDBSelections(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selections: list[VideoEditingDBSelection] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def validate_distinct_candidates(self) -> VideoEditingDBSelections:
        keys = [selection.candidate_key for selection in self.selections]
        if len(set(keys)) != len(keys):
            raise ValueError("recommendation candidate keys must be distinct")
        return self


GraphMode = Literal["TURN", "RECOMMEND"]


class ShortformGraphState(TypedDict, total=False):
    mode: GraphMode
    domain_context: str
    store_context: dict[str, Any]
    project_state: dict[str, Any]
    conversation: list[dict[str, str]]
    user_input: dict[str, Any]
    photo_urls: list[str]
    video_editing_db_candidates: list[dict[str, Any]]
    decision: dict[str, Any]
    recommendations: dict[str, Any]
