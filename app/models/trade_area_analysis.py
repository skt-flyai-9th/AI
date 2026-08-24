from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TradeAreaAnalysis(Base):
    __tablename__ = "trade_area_analyses"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    template_id: Mapped[str] = mapped_column(String(160), index=True)
    template_version: Mapped[int] = mapped_column(Integer)
    evidence_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
