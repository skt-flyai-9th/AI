from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class ChallengeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    rank: int | None
    name: str
    representative_youtube_url: str | None
    guide_youtube_url: str | None
    format_type: str | None = None
    expected_duration_sec: int | None = None
    shooting_difficulty: str | None = None
    requires_face: bool | None = None
    editing_template_id: str | None = None
    editing_template_version: int | None = None
    automatic_rank: int | None
    automatic_score: float
    lifecycle: str
    kr_affinity: float
    confidence: float
    category: str
    active: bool
    rank_overridden: bool
    name_overridden: bool
    representative_video_overridden: bool
    guide_video_overridden: bool
    updated_at: datetime


class ChallengeListResponse(BaseModel):
    generated_at: datetime | None
    count: int
    results: list[ChallengeRead]


class ChallengeUpdate(BaseModel):
    rank: int | None = Field(default=None, ge=1, le=1000)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    representative_youtube_url: HttpUrl | None = None
    guide_youtube_url: HttpUrl | None = None


class OverrideImportItem(BaseModel):
    challenge_id: str
    rank: int | None = Field(default=None, ge=1, le=1000)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    representative_youtube_url: HttpUrl | None = None
    guide_youtube_url: HttpUrl | None = None
