from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.template_knowledge.source_library import import_provided_template_library
from app.template_knowledge.validation import TemplateCandidateValidator


def seed_template_library(
    db: Session,
    *,
    service: Any | None = None,
) -> dict[str, Any]:
    """Import the user-provided workbooks; no synthetic production seeds are created."""

    validator = getattr(service, "validator", None)
    if not isinstance(validator, TemplateCandidateValidator):
        validator = None
    return import_provided_template_library(db, validator=validator)
