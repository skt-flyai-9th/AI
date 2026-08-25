from __future__ import annotations

import logging
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.shortform.graph import build_shortform_graph
from app.agents.shortform.llm import OpenAIShortformLLM, ShortformLLM, ShortformLLMError
from app.agents.shortform.seeds import seed_packaged_editing_templates
from app.agents.shortform.types import ShortformTurnDecision, TemplateCandidate, TemplateSelection
from app.core.config import get_settings
from app.models.challenge import Challenge
from app.models.editing_template import EditingTemplate
from app.models.shortform_session import ShortformSession
from app.schemas.shortform import (
    FaceExposure,
    FilmingTime,
    NextRecommendationResponse,
    ShortformAction,
    ShortformOption,
    ShortformProjectState,
    ShortformRecommendation,
    ShortformSessionCreateResponse,
    ShortformSessionStatus,
    ShortformTurnInput,
    ShortformTurnResponse,
    ShootingGuideResponse,
    StoreContext,
    TurnInputType,
)


_REQUIRED_FIELDS = (
    "promotion_subject",
    "promotion_objective",
    "filming_time",
    "face_exposure",
)
_FILMING_ORDER = {
    FilmingTime.WITHIN_5M.value: 1,
    FilmingTime.WITHIN_10M.value: 2,
    FilmingTime.WITHIN_20M.value: 3,
    FilmingTime.PLUS_30M.value: 4,
}
logger = logging.getLogger(__name__)


class ShortformDomainError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable


class ShortformAgentService:
    def __init__(self, llm: ShortformLLM | None = None) -> None:
        self.llm = llm or OpenAIShortformLLM()
        self.graph = build_shortform_graph(self.llm)
        self.settings = get_settings()
        self.domain_context = _load_domain_context()

    def create_session(self, db: Session, store_context: StoreContext) -> ShortformSessionCreateResponse:
        session = ShortformSession(
            id=f"sf_{uuid.uuid4().hex}",
            status=ShortformSessionStatus.COLLECTING.value,
            store_id=store_context.store.store_id,
            store_context=store_context.model_dump(mode="json"),
            project_state=_initial_project_state(),
            conversation=[],
            shown_template_ids=[],
            current_recommendation=None,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return ShortformSessionCreateResponse(
            session_id=session.id,
            status=ShortformSessionStatus(session.status),
            assistant_message="오늘 어떤 영상을 찍을까요?",
            options=[
                ShortformOption(id="PROMOTION_GUIDE", label="홍보하고 싶은 게 있어요"),
                ShortformOption(id="FREE_INPUT", label="직접 입력하기"),
            ],
            project_state=ShortformProjectState.model_validate(session.project_state),
        )

    def process_turn(
        self,
        db: Session,
        session_id: str,
        turn_input: ShortformTurnInput,
    ) -> ShortformTurnResponse:
        session = self._get_session(db, session_id)
        if session.status == ShortformSessionStatus.COMPLETED.value:
            raise ShortformDomainError(
                "SHORTFORM_SESSION_COMPLETED",
                "Shortform session is already completed.",
                status_code=409,
            )

        if turn_input.type == TurnInputType.CONFIRM:
            if turn_input.value is True:
                return self._confirm_and_recommend(db, session)
            return self._reject_confirmation(db, session)

        graph_result = self._invoke_graph(
            {
                "mode": "TURN",
                "domain_context": self.domain_context,
                "store_context": session.store_context,
                "project_state": session.project_state,
                "conversation": session.conversation,
                "user_input": turn_input.model_dump(mode="json", exclude_none=True),
                "photo_urls": self._photo_urls(session.store_context),
            }
        )
        decision = ShortformTurnDecision.model_validate(graph_result["decision"])
        project_state = self._merge_project_state(session.project_state, decision)

        # Code, not the LLM, is authoritative for recommendation readiness.
        missing = _missing_required_fields(project_state)
        project_state["missing_required_fields"] = missing
        project_state["store_context_conflicts"] = [
            item.model_dump(mode="json") for item in decision.conflicts
        ]
        project_state["ready_for_confirmation"] = not missing and not decision.conflicts

        action = decision.action
        if action == ShortformAction.RECOMMEND and not project_state.get("brief_confirmed"):
            action = (
                ShortformAction.CONFIRM
                if project_state["ready_for_confirmation"]
                else ShortformAction.ASK
            )
        if action == ShortformAction.CONFIRM and not project_state["ready_for_confirmation"]:
            action = ShortformAction.ASK

        session.project_state = project_state
        session.status = (
            ShortformSessionStatus.CONFIRMING.value
            if action == ShortformAction.CONFIRM
            else ShortformSessionStatus.COLLECTING.value
        )
        session.conversation = _append_conversation(
            session.conversation,
            _turn_input_to_text(turn_input),
            decision.assistant_message,
        )
        db.commit()

        if action == ShortformAction.RECOMMEND and project_state.get("brief_confirmed"):
            return self._recommend(db, session)

        options = [ShortformOption(id=item.id, label=item.label) for item in decision.options]
        return ShortformTurnResponse(
            session_id=session.id,
            action=action,
            assistant_message=decision.assistant_message,
            project_state=ShortformProjectState.model_validate(project_state),
            options=options,
            recommendation=None,
        )

    def next_recommendation(
        self,
        db: Session,
        session_id: str,
    ) -> NextRecommendationResponse:
        session = self._get_session(db, session_id)
        if not session.project_state.get("brief_confirmed"):
            raise ShortformDomainError(
                "SHORTFORM_BRIEF_NOT_CONFIRMED",
                "Project brief must be confirmed before requesting recommendations.",
                status_code=409,
            )
        if not session.current_recommendation:
            raise ShortformDomainError(
                "SHORTFORM_RECOMMENDATION_NOT_FOUND",
                "There is no current recommendation to replace.",
                status_code=409,
            )

        response = self._recommend(
            db,
            session,
            user_event="[UI_EVENT] 다시 추천 받기",
        )
        assert response.recommendation is not None
        return NextRecommendationResponse(
            session_id=session.id,
            recommendation=response.recommendation,
            shown_template_ids=list(session.shown_template_ids or []),
        )

    def delete_session(self, db: Session, session_id: str) -> None:
        session = self._get_session(db, session_id)
        db.delete(session)
        db.commit()

    def get_shooting_guide(
        self,
        db: Session,
        template_id: str,
        version: int,
    ) -> ShootingGuideResponse:
        template = db.get(EditingTemplate, (template_id, version))
        if template is None:
            raise ShortformDomainError(
                "EDITING_TEMPLATE_NOT_FOUND",
                "Editing template was not found.",
                status_code=404,
            )
        guide = dict(template.shooting_guide or {})
        return ShootingGuideResponse(
            template_id=template.template_id,
            version=template.version,
            estimated_shooting_sec=guide.get("estimated_shooting_sec"),
            difficulty=guide.get("difficulty") or template.recommendation_metadata.get("difficulty"),
            scenes=list(guide.get("scenes") or []),
            tasks=list(guide.get("tasks") or []),
        )

    def _confirm_and_recommend(
        self,
        db: Session,
        session: ShortformSession,
    ) -> ShortformTurnResponse:
        project_state = dict(session.project_state or {})
        missing = _missing_required_fields(project_state)
        if missing or project_state.get("store_context_conflicts"):
            raise ShortformDomainError(
                "SHORTFORM_BRIEF_NOT_READY",
                "Project brief is not ready for confirmation.",
                status_code=409,
            )
        if not project_state.get("ready_for_confirmation"):
            raise ShortformDomainError(
                "SHORTFORM_BRIEF_NOT_READY",
                "Project brief must reach the confirmation stage first.",
                status_code=409,
            )

        project_state["brief_confirmed"] = True
        session.project_state = project_state
        session.status = ShortformSessionStatus.RECOMMENDING.value
        session.conversation = _append_conversation(
            session.conversation,
            "[UI_CONFIRM] 이대로 추천받기",
            "",
        )
        db.commit()
        return self._recommend(db, session)

    def _reject_confirmation(
        self,
        db: Session,
        session: ShortformSession,
    ) -> ShortformTurnResponse:
        project_state = dict(session.project_state or {})
        project_state["brief_confirmed"] = False
        project_state["ready_for_confirmation"] = False
        session.project_state = project_state
        session.status = ShortformSessionStatus.COLLECTING.value
        message = "수정하고 싶은 내용을 말씀해주세요. 바뀐 항목만 반영할게요."
        session.conversation = _append_conversation(
            session.conversation,
            "[UI_CONFIRM] 수정하기",
            message,
        )
        db.commit()
        return ShortformTurnResponse(
            session_id=session.id,
            action=ShortformAction.ASK,
            assistant_message=message,
            project_state=ShortformProjectState.model_validate(project_state),
            options=[],
            recommendation=None,
        )

    def _recommend(
        self,
        db: Session,
        session: ShortformSession,
        *,
        user_event: str | None = None,
    ) -> ShortformTurnResponse:
        candidates = self._recommendation_candidates(db, session)
        if not candidates:
            raise ShortformDomainError(
                "NO_COMPATIBLE_ACTIVE_EDITING_TEMPLATE",
                "No compatible ACTIVE editing template is available.",
                status_code=409,
            )

        selection, selected = self._select_template(session, candidates)

        recommendation = ShortformRecommendation(
            recommendation_id=f"rec_{uuid.uuid4().hex}",
            project_title=(selection.project_title or selected.recommendation_title or selected.name).strip(),
            title=(selection.title or selected.recommendation_title or selected.name).strip(),
            concept=(selection.concept or selected.recommendation_concept or selected.name).strip(),
            editing_template_id=selected.editing_template_id,
            editing_template_version=selected.editing_template_version,
        )
        stored = recommendation.model_dump(mode="json")
        stored["internal_reason"] = selection.internal_reason
        session.current_recommendation = stored
        session.status = ShortformSessionStatus.WAITING_RECOMMENDATION_ACTION.value
        shown = list(session.shown_template_ids or [])
        if selected.editing_template_id not in shown:
            shown.append(selected.editing_template_id)
        session.shown_template_ids = shown
        conversation = list(session.conversation or [])
        if user_event:
            conversation.append({"role": "user", "content": user_event})
        conversation.append(
            {
                "role": "assistant",
                "content": f"[RECOMMENDATION] {recommendation.title} — {recommendation.concept}",
            }
        )
        session.conversation = conversation[-40:]
        db.commit()
        return ShortformTurnResponse(
            session_id=session.id,
            action=ShortformAction.RECOMMEND,
            assistant_message=None,
            project_state=ShortformProjectState.model_validate(session.project_state),
            options=[],
            recommendation=recommendation,
        )

    def _recommendation_candidates(
        self,
        db: Session,
        session: ShortformSession,
    ) -> list[TemplateCandidate]:
        """Return compatible unseen candidates, starting a new cycle if needed."""

        for strict in (True, False):
            candidates = self._template_candidates(db, session, strict=strict)
            if candidates:
                return candidates

        # `다시 추천` must not permanently exhaust a small, valid catalogue.
        # Recycle only after every compatible template has already been shown;
        # hard safety and physical constraints are still applied below.
        if session.shown_template_ids:
            original = list(session.shown_template_ids)
            session.shown_template_ids = []
            for strict in (True, False):
                candidates = self._template_candidates(db, session, strict=strict)
                if candidates:
                    return candidates
            session.shown_template_ids = original
        return []

    def _select_template(
        self,
        session: ShortformSession,
        candidates: list[TemplateCandidate],
    ) -> tuple[TemplateSelection, TemplateCandidate]:
        """Prefer contextual selection and preserve availability on selector failure."""

        try:
            graph_result = self._invoke_graph(
                {
                    "mode": "RECOMMEND",
                    "domain_context": self.domain_context,
                    "store_context": session.store_context,
                    "project_state": session.project_state,
                    "conversation": session.conversation,
                    "candidate_templates": [
                        item.model_dump(mode="json") for item in candidates
                    ],
                }
            )
            selection = TemplateSelection.model_validate(graph_result["recommendation"])
            selected = {item.candidate_key: item for item in candidates}.get(
                selection.candidate_key
            )
            if selected is None:
                raise ValueError("selection is outside the candidate pool")
            return selection, selected
        except Exception as exc:
            # Conversation collection still depends on the LLM. Once the brief is
            # confirmed, however, a selector outage must not erase an already-safe
            # deterministic candidate pool.
            selected = candidates[0]
            logger.warning(
                "Shortform recommendation selector failed; using %s (%s)",
                selected.candidate_key,
                type(exc).__name__,
            )
            return (
                TemplateSelection(
                    candidate_key=selected.candidate_key,
                    project_title=f"{selected.recommendation_title or selected.name} 프로젝트",
                    title=selected.recommendation_title or selected.name,
                    concept=selected.recommendation_concept or selected.name,
                    internal_reason="Deterministic fallback after selector failure.",
                ),
                selected,
            )

    def _template_candidates(
        self,
        db: Session,
        session: ShortformSession,
        *,
        strict: bool,
    ) -> list[TemplateCandidate]:
        rows = self._active_template_rows(db)
        latest_by_id: dict[str, EditingTemplate] = {}
        for row in rows:
            latest_by_id.setdefault(row.template_id, row)

        project_state = session.project_state or {}
        shown = set(session.shown_template_ids or [])
        result: list[TemplateCandidate] = []
        for template in latest_by_id.values():
            if template.template_id in shown:
                continue
            metadata = dict(template.recommendation_metadata or {})
            if not _passes_hard_constraints(metadata, project_state):
                continue
            if strict and not _passes_soft_constraints(metadata, project_state):
                continue
            result.append(
                TemplateCandidate(
                    candidate_key=f"{template.template_id}@{template.version}",
                    editing_template_id=template.template_id,
                    editing_template_version=template.version,
                    name=template.name,
                    recommendation_title=template.recommendation_title or template.name,
                    recommendation_concept=template.recommendation_concept or template.name,
                    recommendation_metadata=metadata,
                    trend_context=self._trend_context(db, template.trend_ids or []),
                )
            )
        return result

    @staticmethod
    def _active_template_rows(db: Session) -> list[EditingTemplate]:
        def load() -> list[EditingTemplate]:
            return list(
                db.scalars(
                    select(EditingTemplate)
                    .where(EditingTemplate.status == "ACTIVE")
                    .order_by(
                        EditingTemplate.template_id.asc(),
                        EditingTemplate.version.desc(),
                    )
                )
            )

        rows = load()
        if rows:
            return rows
        seed_packaged_editing_templates(db)
        return load()

    def _trend_context(self, db: Session, trend_ids: list[str]) -> list[dict[str, Any]]:
        if trend_ids:
            rows = list(db.scalars(select(Challenge).where(Challenge.id.in_(trend_ids))))
        else:
            rows = list(
                db.scalars(
                    select(Challenge)
                    .where(Challenge.active.is_(True))
                    .order_by(
                        Challenge.automatic_rank.asc().nullslast(),
                        Challenge.automatic_score.desc(),
                    )
                    .limit(5)
                )
            )
        return [
            {
                "trend_id": row.id,
                "name": row.override_name if row.name_overridden else row.automatic_name,
                "lifecycle": row.lifecycle,
                "korea_relevance": row.kr_affinity,
                "confidence": row.confidence,
                "representative_youtube_url": (
                    row.override_representative_youtube_url
                    if row.representative_video_overridden
                    else row.automatic_representative_youtube_url
                ),
            }
            for row in rows
        ]

    def _merge_project_state(
        self,
        current: dict[str, Any],
        decision: ShortformTurnDecision,
    ) -> dict[str, Any]:
        state = dict(current or {})
        updates = decision.state_updates

        if updates.promotion_category is not None:
            state["promotion_category"] = updates.promotion_category
        if updates.promotion_subject is not None:
            state["promotion_subject"] = {
                "type": updates.promotion_subject.type,
                "name": updates.promotion_subject.name,
                "menu_id": updates.promotion_subject.menu_id,
                "details": {item.key: item.value for item in updates.promotion_subject.details},
            }
        if updates.promotion_objective is not None:
            state["promotion_objective"] = updates.promotion_objective.value
        if updates.filming_time is not None:
            state["filming_time"] = updates.filming_time.value
        if updates.face_exposure is not None:
            state["face_exposure"] = updates.face_exposure.value

        for field, values in (
            ("creative_preferences", updates.creative_preferences),
            ("secondary_information", updates.secondary_information),
        ):
            existing = list(state.get(field) or [])
            for value in values:
                if value and value not in existing:
                    existing.append(value)
            state[field] = existing

        facts = dict(state.get("facts_from_user") or {})
        facts.update({item.key: item.value for item in updates.facts_from_user})
        state["facts_from_user"] = facts
        return state

    def _photo_urls(self, store_context: dict[str, Any]) -> list[str]:
        photos = ((store_context.get("store") or {}).get("store_photos") or [])
        urls = [
            str(item.get("asset_url") or "").strip()
            for item in photos
            if isinstance(item, dict)
        ]
        return [url for url in urls if url][: self.settings.shortform_max_photo_inputs]

    def _invoke_graph(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.graph.invoke(state)
        except ShortformLLMError:
            raise
        except ShortformDomainError:
            raise
        except Exception as exc:
            raise ShortformDomainError(
                "SHORTFORM_AGENT_EXECUTION_FAILED",
                "Shortform Agent graph execution failed.",
                status_code=500,
                retryable=True,
            ) from exc

    @staticmethod
    def _get_session(db: Session, session_id: str) -> ShortformSession:
        session = db.get(ShortformSession, session_id)
        if session is None:
            raise ShortformDomainError(
                "SHORTFORM_SESSION_NOT_FOUND",
                "Shortform session was not found.",
                status_code=404,
            )
        return session


def _initial_project_state() -> dict[str, Any]:
    return ShortformProjectState(
        missing_required_fields=list(_REQUIRED_FIELDS),
        ready_for_confirmation=False,
        brief_confirmed=False,
    ).model_dump(mode="json")


def _missing_required_fields(state: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    subject = state.get("promotion_subject")
    if not isinstance(subject, dict) or not str(subject.get("name") or "").strip():
        missing.append("promotion_subject")
    for field in ("promotion_objective", "filming_time", "face_exposure"):
        if not state.get(field):
            missing.append(field)
    return missing


def _passes_hard_constraints(metadata: dict[str, Any], state: dict[str, Any]) -> bool:
    if metadata.get("renderer_supported") is False:
        return False
    if metadata.get("requires_tts") is True:
        return False
    if metadata.get("requires_photo_input") is True:
        return False

    face_mode = state.get("face_exposure")
    if face_mode == FaceExposure.NOT_ALLOWED.value:
        if metadata.get("requires_face") is True:
            return False
        allowed_faces = metadata.get("supported_face_modes") or []
        if allowed_faces and FaceExposure.NOT_ALLOWED.value not in allowed_faces:
            return False

    minimum = metadata.get("minimum_filming_time")
    current = state.get("filming_time")
    if minimum in _FILMING_ORDER and current in _FILMING_ORDER:
        if _FILMING_ORDER[minimum] > _FILMING_ORDER[current]:
            return False
    return True


def _passes_soft_constraints(metadata: dict[str, Any], state: dict[str, Any]) -> bool:
    objective = state.get("promotion_objective")
    supported_objectives = metadata.get("supported_objectives") or []
    if supported_objectives and objective not in supported_objectives:
        return False

    subject = state.get("promotion_subject") or {}
    subject_type = str(subject.get("type") or "").upper()
    supported_subject_types = [
        str(value).upper() for value in metadata.get("supported_subject_types") or []
    ]
    if supported_subject_types and subject_type and subject_type not in supported_subject_types:
        return False

    filming_time = state.get("filming_time")
    supported_times = metadata.get("supported_filming_times") or []
    if supported_times and filming_time not in supported_times:
        return False
    return True


def _append_conversation(
    conversation: list[dict[str, str]] | None,
    user_text: str,
    assistant_text: str,
) -> list[dict[str, str]]:
    items = list(conversation or [])
    if user_text:
        items.append({"role": "user", "content": user_text})
    if assistant_text:
        items.append({"role": "assistant", "content": assistant_text})
    return items[-40:]


def _turn_input_to_text(turn_input: ShortformTurnInput) -> str:
    if turn_input.type == TurnInputType.TEXT:
        return turn_input.text or ""
    if turn_input.type == TurnInputType.OPTION:
        return f"[OPTION] {turn_input.option_id}"
    return f"[CONFIRM] {turn_input.value}"


@lru_cache(maxsize=1)
def _load_domain_context() -> str:
    return Path(__file__).with_name("context.md").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_shortform_agent_service() -> ShortformAgentService:
    return ShortformAgentService()
