from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal
from typing_extensions import TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.editing import EditRecipe, PublishingResult


class VideoKeyframe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_index: int = Field(default=0, ge=0)
    timestamp_ms: int = Field(ge=0)
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


class FrameObservation(BaseModel):
    """One-frame semantic/geometry observation used to match Gemini evidence."""

    model_config = ConfigDict(extra="forbid")

    video_id: str
    frame_index: int = Field(ge=0)
    timestamp_ms: int = Field(ge=0)
    semantic_event: str = "NONE"
    subject: str = ""
    subject_x: float | None = Field(default=None, ge=0, le=1)
    subject_y: float | None = Field(default=None, ge=0, le=1)
    subject_scale: float | None = Field(default=None, ge=0, le=1)
    action: str = ""
    action_phase: Literal["NONE", "START", "MIDDLE", "END", "HOLD"] = "NONE"
    composition: str = ""
    camera_motion: str = ""
    motion_direction: str = ""
    motion_strength: float = Field(default=0.0, ge=0, le=1)
    observed_rotation_deg: float = Field(default=0.0, ge=-45, le=45)
    observed_zoom_scale: float | None = Field(default=None, ge=0.5, le=2.0)
    observed_translate_x_pct: float = Field(default=0.0, ge=-1.0, le=1.0)
    observed_translate_y_pct: float = Field(default=0.0, ge=-1.0, le=1.0)
    flash_level: float = Field(default=0.0, ge=0, le=1)
    color_tone: Literal["NATURAL", "WARM", "COOL", "VIVID", "UNKNOWN"] = "UNKNOWN"
    cut_transition_candidate: bool = False
    cut_transition_score: float = Field(default=0.0, ge=0, le=1)
    quality_flags: list[str] = Field(default_factory=list, max_length=10)
    mapped_reference_segment_id: str | None = None
    produced_timestamp_ms: int | None = Field(default=None, ge=0)


class FrameBatchAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=2000)
    observations: list[FrameObservation] = Field(min_length=1)


class SourceCutDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    trim_in_ms: int = Field(ge=0)
    trim_out_ms: int = Field(gt=0)
    mapped_reference_segment_id: str
    cut_in_reason: str = Field(max_length=500)
    cut_out_reason: str = Field(max_length=500)
    decision_reason: str = Field(max_length=1000)

    @model_validator(mode="after")
    def validate_range(self) -> SourceCutDecision:
        if self.trim_in_ms >= self.trim_out_ms:
            raise ValueError("trim_in_ms must be before trim_out_ms")
        return self


class SourceCutPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["MULTI_CUT"] = "MULTI_CUT"
    strategy: Literal["CUT_PER_INPUT", "INFORMATIONAL_REASSEMBLY"] = "CUT_PER_INPUT"
    cuts: list[SourceCutDecision] = Field(min_length=1)
    rationale: str = Field(max_length=2000)

    @model_validator(mode="after")
    def validate_non_overlapping_source_ranges(self) -> SourceCutPlan:
        reference_ids = [item.mapped_reference_segment_id for item in self.cuts]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("each reference segment must be assigned exactly once")
        by_video: dict[str, list[SourceCutDecision]] = {}
        for item in self.cuts:
            by_video.setdefault(item.video_id, []).append(item)
        for video_id, cuts in by_video.items():
            ordered = sorted(cuts, key=lambda item: (item.trim_in_ms, item.trim_out_ms))
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if current.trim_in_ms < previous.trim_out_ms:
                    raise ValueError(
                        f"source ranges must not overlap for video_id={video_id}"
                    )
        return self


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
    video_editing_db: dict[str, Any]
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
    stage_callback: Callable[[str, int], None]
    checkpoint_callback: Callable[[dict[str, Any]], None]


def persistable_video_context(context: VideoContext) -> dict[str, Any]:
    """Drop base64 images before DB persistence while retaining frame evidence."""
    data = context.model_dump(mode="json")
    data["keyframes"] = [
        {
            "frame_index": item["frame_index"],
            "timestamp_ms": item["timestamp_ms"],
        }
        for item in data.get("keyframes", [])
    ]
    return data
