from __future__ import annotations

from typing import Any, Literal
from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.editing import EditRecipe, PublishingResult


class VideoKeyframe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp_ms: int
    image_url: str


class VideoContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    shooting_scene_order: int
    duration_ms: int
    width: int
    height: int
    fps: float
    keyframes: list[VideoKeyframe]


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    path: str
    message: str
    source: Literal["DOMAIN", "REALS_REGISTRY"]
    repairable: bool = True


class EditingPlanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["RECIPE", "SOURCE_GAP"]
    recipe: EditRecipe | None
    publishing: PublishingResult | None
    missing_scene_roles: list[str]
    available_options: list[Literal["USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"]]
    rationale: str

    @model_validator(mode="after")
    def validate_outcome(self) -> EditingPlanDecision:
        if self.outcome == "RECIPE":
            if self.recipe is None or self.publishing is None:
                raise ValueError("RECIPE outcome requires recipe and publishing")
            if self.missing_scene_roles or self.available_options:
                raise ValueError("RECIPE outcome cannot contain source-gap fields")
        else:
            if self.recipe is not None or self.publishing is not None:
                raise ValueError("SOURCE_GAP outcome cannot contain recipe or publishing")
            if not self.missing_scene_roles:
                raise ValueError("SOURCE_GAP requires missing_scene_roles")
            required = {"USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"}
            if set(self.available_options) != required:
                raise ValueError("SOURCE_GAP must return both available options")
        return self


class EditingGraphState(TypedDict, total=False):
    domain_context: str
    project: dict[str, Any]
    selected_shortform: dict[str, Any]
    template: dict[str, Any]
    videos: list[dict[str, Any]]
    video_contexts: list[dict[str, Any]]
    parent_recipe: dict[str, Any] | None
    revision_action: str | None
    decision: dict[str, Any]
    validation_errors: list[dict[str, Any]]
    validation_passed: bool
    repair_attempts: int
    max_repair_attempts: int
    exhausted: bool


def persistable_video_context(context: VideoContext) -> dict[str, Any]:
    """Drop base64 images before DB persistence while retaining timestamp evidence."""
    data = context.model_dump(mode="json")
    data["keyframes"] = [
        {"timestamp_ms": item["timestamp_ms"]} for item in data.get("keyframes", [])
    ]
    return data
