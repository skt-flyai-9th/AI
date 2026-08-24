from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class VideoEditingDBRecord(Base):
    __tablename__ = "video_editing_db_records"

    template_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    name: Mapped[str] = mapped_column(String(255))
    recommendation_title: Mapped[str] = mapped_column(String(255), default="")
    recommendation_concept: Mapped[str] = mapped_column(Text, default="")
    recommendation_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    shooting_guide: Mapped[dict] = mapped_column(JSON, default=dict)
    editing_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    trend_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    source_candidate_id: Mapped[str | None] = mapped_column(
        String(48), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
