from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TemplateVideoAnalysis(Base):
    __tablename__ = "template_video_analyses"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    trend_id: Mapped[str] = mapped_column(String(160), index=True)
    youtube_url: Mapped[str] = mapped_column(Text)
    source_fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    model: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(24), index=True)
    insights: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
