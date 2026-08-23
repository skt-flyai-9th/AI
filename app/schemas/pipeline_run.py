from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class PipelineRunCreateResponse(BaseModel):
    run_id: str
    status: str
    task_id: str | None = None


class PipelineRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    stage: str
    progress: int
    celery_task_id: str | None
    error_message: str | None
    warnings: list
    source_status: dict
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class PipelineRunResultItem(BaseModel):
    id: str
    rank: int
    name: str
    representative_youtube_url: str | None
    guide_youtube_url: str | None


class PipelineRunResultResponse(BaseModel):
    run_id: str
    status: str
    generated_at: datetime | None
    count: int
    warnings: list[str]
    results: list[PipelineRunResultItem]
