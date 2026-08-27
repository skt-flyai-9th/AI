from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.agents.challenge_ranking.trendcluster import get_video_format_metadata
from app.models.challenge import Challenge
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.schemas.challenge import ChallengeRead, ChallengeUpdate, OverrideImportItem


def effective_rank_expression():
    return case(
        (Challenge.rank_overridden.is_(True), Challenge.override_rank),
        else_=Challenge.automatic_rank,
    )


def active_template_refs(
    db: Session,
    challenge_ids: set[str],
) -> dict[str, tuple[str, int]]:
    if not challenge_ids:
        return {}

    rows = list(
        db.scalars(
            select(VideoEditingDBRecord)
            .where(VideoEditingDBRecord.status == "ACTIVE")
            .order_by(VideoEditingDBRecord.version.desc())
        )
    )
    refs: dict[str, tuple[str, int]] = {}
    for row in rows:
        for trend_id in row.trend_ids or []:
            if trend_id in challenge_ids:
                refs.setdefault(trend_id, (row.template_id, row.version))
    return refs


def to_read(
    challenge: Challenge,
    template_ref: tuple[str, int] | None = None,
) -> ChallengeRead:
    rank = challenge.override_rank if challenge.rank_overridden else challenge.automatic_rank
    name = challenge.override_name if challenge.name_overridden else challenge.automatic_name
    representative = (
        challenge.override_representative_youtube_url
        if challenge.representative_video_overridden
        else challenge.automatic_representative_youtube_url
    )
    guide = (
        challenge.override_guide_youtube_url
        if challenge.guide_video_overridden
        else challenge.automatic_guide_youtube_url
    )
    format_metadata = get_video_format_metadata(challenge.id)
    return ChallengeRead(
        id=challenge.id,
        rank=rank,
        name=name,
        representative_youtube_url=representative,
        guide_youtube_url=guide,
        **format_metadata,
        editing_template_id=template_ref[0] if template_ref else None,
        editing_template_version=template_ref[1] if template_ref else None,
        automatic_rank=challenge.automatic_rank,
        automatic_score=challenge.automatic_score,
        lifecycle=challenge.lifecycle,
        kr_affinity=challenge.kr_affinity,
        confidence=challenge.confidence,
        category=challenge.category,
        active=challenge.active,
        rank_overridden=challenge.rank_overridden,
        name_overridden=challenge.name_overridden,
        representative_video_overridden=challenge.representative_video_overridden,
        guide_video_overridden=challenge.guide_video_overridden,
        updated_at=challenge.updated_at,
    )


def list_challenges(db: Session, *, limit: int, offset: int, include_inactive: bool) -> list[Challenge]:
    stmt = select(Challenge)
    if not include_inactive:
        stmt = stmt.where(Challenge.active.is_(True))
    return list(
        db.scalars(
            stmt.order_by(effective_rank_expression().asc().nullslast(), Challenge.automatic_score.desc())
            .offset(offset)
            .limit(limit)
        )
    )


def get_latest_generated_at(db: Session) -> datetime | None:
    return db.scalar(select(func.max(Challenge.last_seen_at)).where(Challenge.active.is_(True)))


def apply_update(challenge: Challenge, payload: ChallengeUpdate) -> Challenge:
    fields = payload.model_fields_set
    if "rank" in fields:
        challenge.override_rank = payload.rank
        challenge.rank_overridden = payload.rank is not None
    if "name" in fields:
        challenge.override_name = payload.name
        challenge.name_overridden = payload.name is not None
    if "representative_youtube_url" in fields:
        challenge.override_representative_youtube_url = (
            str(payload.representative_youtube_url) if payload.representative_youtube_url else None
        )
        challenge.representative_video_overridden = payload.representative_youtube_url is not None
    if "guide_youtube_url" in fields:
        challenge.override_guide_youtube_url = (
            str(payload.guide_youtube_url) if payload.guide_youtube_url else None
        )
        challenge.guide_video_overridden = payload.guide_youtube_url is not None
    return challenge


def import_override_items(db: Session, items: list[OverrideImportItem]) -> tuple[int, list[str]]:
    updated = 0
    missing: list[str] = []
    for item in items:
        challenge = db.get(Challenge, item.challenge_id)
        if challenge is None:
            missing.append(item.challenge_id)
            continue
        payload = ChallengeUpdate(
            rank=item.rank,
            name=item.name,
            representative_youtube_url=item.representative_youtube_url,
            guide_youtube_url=item.guide_youtube_url,
        )
        # Import files are explicit snapshots; null clears an override.
        payload.__pydantic_fields_set__ = {
            "rank", "name", "representative_youtube_url", "guide_youtube_url"
        }
        apply_update(challenge, payload)
        updated += 1
    db.commit()
    return updated, missing
