from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.editing_template import EditingTemplate
from app.models.template_update_candidate import TemplateUpdateCandidate
from app.schemas.template_knowledge import EditingCandidateCreate, TemplateCandidateStatus
from app.template_knowledge.service import (
    TemplateKnowledgeDomainError,
    TemplateKnowledgeService,
)


def run_scheduled_template_maintenance(
    db: Session,
    *,
    service: TemplateKnowledgeService | None = None,
) -> dict:
    settings = get_settings()
    if not settings.template_maintenance_enabled:
        return {"status": "DISABLED", "created": [], "skipped": [], "failures": []}
    manager = service or TemplateKnowledgeService()
    active_ids = list(
        db.scalars(
            select(EditingTemplate.template_id)
            .where(EditingTemplate.status == "ACTIVE")
            .distinct()
            .order_by(EditingTemplate.template_id)
        )
    )
    pending_ids = set(
        db.scalars(
            select(TemplateUpdateCandidate.template_id).where(
                TemplateUpdateCandidate.template_type == "VIDEO_EDITING",
                TemplateUpdateCandidate.status.in_(
                    [
                        TemplateCandidateStatus.GENERATED.value,
                        TemplateCandidateStatus.VALIDATED.value,
                        TemplateCandidateStatus.APPROVED.value,
                    ]
                ),
            )
        )
    )
    created: list[str] = []
    skipped: list[str] = []
    failures: list[dict[str, str]] = []
    for template_id in active_ids:
        if template_id in pending_ids:
            skipped.append(template_id)
            continue
        try:
            candidate = manager.create_editing_candidate(
                db,
                EditingCandidateCreate(
                    template_id=template_id,
                    trend_ids=[],
                    requires_human_approval=True,
                ),
            )
            created.append(candidate.id)
        except TemplateKnowledgeDomainError as exc:
            failures.append({"template_id": template_id, "code": exc.code, "message": str(exc)})
    return {
        "status": "COMPLETED",
        "created": created,
        "skipped": skipped,
        "failures": failures,
    }
