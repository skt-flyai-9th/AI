from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TradeAreaDBRecord(Base):
    __tablename__ = "trade_area_db_records"

    template_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="DRAFT", index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    industry_categories: Mapped[list] = mapped_column(JSON, default=list)
    area_types: Mapped[list] = mapped_column(JSON, default=list)
    analysis_dimensions: Mapped[list] = mapped_column(JSON, default=list)
    inference_rules: Mapped[list] = mapped_column(JSON, default=list)
    recommendation_hints: Mapped[list] = mapped_column(JSON, default=list)
    prompt_context: Mapped[str] = mapped_column(Text, default="")
    policy: Mapped[dict] = mapped_column(JSON, default=dict)
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
