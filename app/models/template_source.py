from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TemplateSourceBundle(Base):
    __tablename__ = "template_source_bundles"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    template_type: Mapped[str] = mapped_column(String(32), index=True)
    schema_version: Mapped[str] = mapped_column(String(32))
    source_filename: Mapped[str] = mapped_column(String(255))
    source_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    dataset_manifest: Mapped[dict] = mapped_column(JSON, default=dict)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class TemplateSourceRecord(Base):
    __tablename__ = "template_source_records"
    __table_args__ = (
        UniqueConstraint(
            "bundle_id",
            "dataset_name",
            "source_row_number",
            name="uq_template_source_record_location",
        ),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    bundle_id: Mapped[str] = mapped_column(
        ForeignKey("template_source_bundles.id", ondelete="CASCADE"), index=True
    )
    dataset_name: Mapped[str] = mapped_column(String(120), index=True)
    record_key: Mapped[str] = mapped_column(String(255), index=True)
    source_row_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
