from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class StoreTradeAreaInsight(Base):
    """Persisted store-level result produced by the trade-area insight endpoint."""

    __tablename__ = "store_trade_area_insights"

    normalized_address: Mapped[str] = mapped_column(String(320), primary_key=True)
    address: Mapped[str] = mapped_column(String(255))
    store_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    district_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
