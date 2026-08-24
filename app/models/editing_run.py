from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class EditingRun(Base):
    __tablename__ = "editing_runs"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    parent_run_id: Mapped[str | None] = mapped_column(
        String(48), ForeignKey("editing_runs.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="QUEUED")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warnings: Mapped[list] = mapped_column(JSON, default=list)

    request_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    video_context: Mapped[list] = mapped_column(JSON, default=list)
    recipe: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    render_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    publishing_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    missing_scene_roles: Mapped[list] = mapped_column(JSON, default=list)
    available_options: Mapped[list] = mapped_column(JSON, default=list)
    revision_action: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
