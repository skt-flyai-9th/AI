from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Challenge(Base):
    __tablename__ = "challenges"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    automatic_name: Mapped[str] = mapped_column(String(255), index=True)
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(80), default="unknown")

    automatic_rank: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    automatic_score: Mapped[float] = mapped_column(Float, default=0.0)
    lifecycle: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    kr_affinity: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)

    automatic_representative_youtube_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    automatic_guide_youtube_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    representative_video_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    guide_video_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    override_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    override_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    override_representative_youtube_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_guide_youtube_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    rank_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    name_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    representative_video_overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    guide_video_overridden: Mapped[bool] = mapped_column(Boolean, default=False)

    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    latest_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    raw_details: Mapped[dict] = mapped_column(JSON, default=dict)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    snapshots = relationship("RankingSnapshot", back_populates="challenge")
