from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TemplateUpdateCandidate(Base):
    __tablename__ = "template_update_candidates"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    template_type: Mapped[str] = mapped_column(String(32), index=True)
    template_id: Mapped[str] = mapped_column(String(160), index=True)
    base_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposed_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="GENERATED", index=True)
    source_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    proposed_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    diff: Mapped[list] = mapped_column(JSON, default=list)
    validation_errors: Mapped[list] = mapped_column(JSON, default=list)
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    generation_model: Mapped[str] = mapped_column(String(160), default="")
    approved_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    approval_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
