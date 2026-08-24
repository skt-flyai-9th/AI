from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ShortformSession(Base):
    __tablename__ = "shortform_sessions"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    status: Mapped[str] = mapped_column(String(40), default="COLLECTING", index=True)
    store_id: Mapped[str] = mapped_column(String(160), index=True)

    # The AI server owns conversational state. The backend only needs session_id,
    # but may cache project_state returned by the public API.
    store_context: Mapped[dict] = mapped_column(JSON, default=dict)
    project_state: Mapped[dict] = mapped_column(JSON, default=dict)
    conversation: Mapped[list] = mapped_column(JSON, default=list)
    shown_video_editing_db_ids: Mapped[list] = mapped_column(JSON, default=list)
    current_recommendation: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
