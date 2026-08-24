from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TemplateKnowledgeRun(Base):
    __tablename__ = "template_knowledge_runs"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    operation: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    stage: Mapped[str] = mapped_column(String(64), default="QUEUED")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
