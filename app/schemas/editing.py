from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_OPERATIONAL_COPY_MARKERS = (
    "플랫폼에서 직접",
    "직접 추가",
    "업로드 후",
    "게시 시",
    "음원은",
    "음악은",
)


def _reject_operational_copy(value: str) -> str:
    text = value.strip()
    if any(marker in text for marker in _OPERATIONAL_COPY_MARKERS):
        raise ValueError("operational music/upload instructions are not publishing copy")
    return text


class EditingRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    SOURCE_GAP = "SOURCE_GAP"
    FAILED = "FAILED"


class EditingRunStage(StrEnum):
    QUEUED = "QUEUED"
    PREPARING_VIDEO_CONTEXT = "PREPARING_VIDEO_CONTEXT"
    PLANNING_RECIPE = "PLANNING_RECIPE"
    VALIDATING_RECIPE = "VALIDATING_RECIPE"
    RENDERING = "RENDERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EditingProject(BaseModel):
    model_config = ConfigDict(extra="allow")

    project_id: str
    store_id: str
    promotion_subject: dict[str, Any]
    promotion_objective: str
    face_exposure: str


class SelectedShortform(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation_id: str
    editing_template_id: str
    editing_template_version: int = Field(ge=1)


class EditingVideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str
    footage_url: str
    shooting_scene_order: int | None = Field(default=None, ge=1)
    # Deprecated rolling-deploy input only. The service converts a complete
    # legacy list to shooting_scene_order and never forwards this to planning.
    shooting_element_id: str | None = Field(
        default=None,
        pattern=r"^ELEMENT_[0-9]{2}$",
    )


class EditingRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: EditingProject
    selected_shortform: SelectedShortform
    videos: list[EditingVideoInput] = Field(min_length=1, max_length=20)
    revision: str | None = None

    @model_validator(mode="after")
    def validate_unique_videos(self) -> EditingRunCreateRequest:
        video_ids = [video.video_id for video in self.videos]
        if len(video_ids) != len(set(video_ids)):
            raise ValueError("video_id must be unique")
        orders = [
            video.shooting_scene_order
            for video in self.videos
            if video.shooting_scene_order is not None
        ]
        if len(orders) != len(set(orders)):
            raise ValueError("shooting_scene_order must be unique")
        return self


class EditingRunCreateResponse(BaseModel):
    run_id: str
    status: EditingRunStatus
    task_id: str | None = None
    parent_run_id: str | None = None


class EditingRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    parent_run_id: str | None
    status: EditingRunStatus
    stage: EditingRunStage
    progress: int
    celery_task_id: str | None
    error_message: str | None
    warnings: list[str]
    queue_position: int | None = None
    estimated_wait_sec: int | None = None
    stage_elapsed_sec: int = 0
    recovery_attempts: int = 0
    llm_request_count: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_estimated_cost_usd: float = 0.0
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RecipeEffectParams(BaseModel):
    """Renderer-safe parameters for frame-timed transform effects."""

    model_config = ConfigDict(extra="forbid")

    start_ms: int | None = Field(default=None, ge=0, le=60_000)
    end_ms: int | None = Field(default=None, ge=0, le=60_000)
    scale_start: float | None = Field(default=None, ge=1.0, le=1.2)
    scale_end: float | None = Field(default=None, ge=1.0, le=1.2)
    scale: float | None = Field(default=None, ge=1.0, le=1.05)
    amplitude_x_pct: float | None = Field(default=None, ge=0.0, le=0.03)
    amplitude_y_pct: float | None = Field(default=None, ge=0.0, le=0.03)
    rotation_deg: float | None = Field(default=None, ge=-3.0, le=3.0)
    translate_x_pct: float | None = Field(default=None, ge=-0.08, le=0.08)
    translate_y_pct: float | None = Field(default=None, ge=-0.08, le=0.08)
    frequency_hz: float | None = Field(default=None, ge=1.0, le=30.0)
    damping: bool | None = None
    opacity: float | None = Field(default=None, ge=0.0, le=1.0)
    tone: Literal["NATURAL", "WARM", "COOL", "VIVID"] | None = None

    @model_validator(mode="after")
    def validate_window(self) -> RecipeEffectParams:
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("effect start_ms and end_ms must be supplied together")
        if self.start_ms is not None and self.end_ms is not None and self.start_ms >= self.end_ms:
            raise ValueError("effect start_ms must be before end_ms")
        return self


class RecipeEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    effect_id: str
    params: RecipeEffectParams = Field(default_factory=RecipeEffectParams)


class RecipeCaption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=80)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    position: Literal["BOTTOM", "MIDDLE", "TOP"] = "BOTTOM"
    style_id: str = "CAPTION"
    motion_id: Literal["NONE", "FADE", "POP", "TYPEWRITER"] = "NONE"
    font_weight: Literal["REGULAR", "SEMIBOLD", "BOLD"] = "SEMIBOLD"
    scale: float = Field(default=1.0, ge=0.8, le=1.5)


class RecipeClip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_order: int = Field(ge=1)
    video_id: str
    source_start_ms: int = Field(ge=0)
    source_end_ms: int = Field(gt=0)
    timeline_start_ms: int = Field(ge=0)
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    crop_mode: Literal["KEEP", "SUBJECT_CENTER", "CENTER_9_16"] = "SUBJECT_CENTER"
    transition_in: Literal["CUT", "HARD_CUT", "FLASH_WHITE"] | None = None
    transition_out: Literal["CUT", "HARD_CUT", "FLASH_WHITE"] | None = "CUT"
    caption: RecipeCaption | None = None
    effects: list[RecipeEffect] = Field(default_factory=list)


class RecipeCta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=80)

    _validate_text = field_validator("text")(_reject_operational_copy)


class EditRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_version: int = Field(default=1, ge=1)
    editing_template_id: str
    editing_template_version: int = Field(ge=1)
    source_type: Literal["VIDEO_ONLY"] = "VIDEO_ONLY"
    timeline: list[RecipeClip] = Field(min_length=1)
    cta: RecipeCta


class EditingRenderResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_video_url: str
    resolution: str
    duration_sec: float = Field(gt=0)
    cover_image_url: str | None = None


class PublishingTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["FIXED", "SUGGESTED"]
    title: str | None = Field(default=None, max_length=120)
    artist: str | None = Field(default=None, max_length=120)
    # The source-song offset is intentionally not inferred until audio matching exists.
    start_sec: None = None
    end_sec: None = None
    mood: str | None = Field(default=None, max_length=160)
    search_keyword: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_mode(self) -> PublishingTrack:
        if self.mode == "FIXED":
            if not self.title:
                raise ValueError("FIXED track requires a verified title")
        else:
            if self.title is not None or self.artist is not None:
                raise ValueError("SUGGESTED track cannot claim a verified title or artist")
            if not self.search_keyword:
                raise ValueError("SUGGESTED track requires a platform search_keyword")
        return self


class PublishingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=80)
    caption: str = Field(min_length=1, max_length=2000)
    hashtags: list[str] = Field(min_length=5, max_length=20)
    track: PublishingTrack
    post_note: str = "음원은 게시 시 플랫폼 내에서 추가해주세요."

    _validate_title = field_validator("title")(_reject_operational_copy)
    _validate_caption = field_validator("caption")(_reject_operational_copy)

    @field_validator("hashtags")
    @classmethod
    def validate_hashtags(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("hashtags must be unique")
        for value in values:
            if value != value.strip() or not value.startswith("#") or len(value) == 1:
                raise ValueError("each hashtag must be a non-empty value beginning with #")
            if any(character.isspace() for character in value):
                raise ValueError("hashtags cannot contain whitespace")
        return values

    @model_validator(mode="after")
    def validate_track_note(self) -> PublishingResult:
        if (
            self.track.mode == "SUGGESTED"
            and self.track.search_keyword
            and self.track.search_keyword not in self.post_note
        ):
            raise ValueError("post_note must include the suggested track search_keyword")
        return self


class EditingRunResultResponse(BaseModel):
    run_id: str
    status: EditingRunStatus
    recipe: EditRecipe | None = None
    render: EditingRenderResult | None = None
    publishing: PublishingResult | None = None
    warnings: list[str] = Field(default_factory=list)
    missing_scene_roles: list[str] = Field(default_factory=list)
    available_options: list[Literal["USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"]] = Field(
        default_factory=list
    )


class EditingRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_action: str = Field(min_length=1, max_length=1000)
    videos: list[EditingVideoInput] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_unique_videos(self) -> EditingRevisionRequest:
        video_ids = [video.video_id for video in self.videos]
        if len(video_ids) != len(set(video_ids)):
            raise ValueError("video_id must be unique")
        orders = [
            video.shooting_scene_order
            for video in self.videos
            if video.shooting_scene_order is not None
        ]
        if len(orders) != len(set(orders)):
            raise ValueError("shooting_scene_order must be unique")
        return self


class EditingRevisionResponse(BaseModel):
    run_id: str
    parent_run_id: str
    status: EditingRunStatus
    task_id: str | None = None
