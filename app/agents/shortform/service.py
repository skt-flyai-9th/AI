from __future__ import annotations

import logging
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.shortform.graph import build_shortform_graph
from app.agents.shortform.harness import shortform_harness
from app.agents.shortform.llm import OpenAIShortformLLM, ShortformLLM, ShortformLLMError
from app.agents.shortform.types import (
    DecisionOption,
    ShortformTurnDecision,
    VideoEditingDBCandidate,
    VideoEditingDBSelection,
    VideoEditingDBSelections,
)
from app.core.config import get_settings
from app.models.challenge import Challenge
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.models.shortform_session import ShortformSession
from app.schemas.shortform import (
    FaceExposure,
    FILMING_TIME_BUCKET_SECONDS,
    FilmingTime,
    NextRecommendationResponse,
    PromotionCategory,
    PromotionObjective,
    ShortformAction,
    ShortformEntryMode,
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
from app.schemas.template_knowledge import MAX_SHOOTING_GUIDE_TITLE_CHARS
from app.services.store_trade_area_context import enrich_store_context_with_trade_area
from app.template_knowledge.seeds import seed_template_library


_REQUIRED_FIELDS = (
    "promotion_subject",
    "filming_time",
    "face_exposure",
)
_REMOVED_PROMOTION_OBJECTIVE_OPTION_IDS = {
    item.value for item in PromotionObjective
} | {"direct_input"}
_FILMING_ORDER = {
    FilmingTime.WITHIN_5M.value: 1,
    FilmingTime.WITHIN_10M.value: 2,
    FilmingTime.WITHIN_20M.value: 3,
    FilmingTime.PLUS_30M.value: 4,
}
_CandidateConstraintMode = Literal["strict", "safe", "any"]
logger = logging.getLogger(__name__)
_QUESTION_ACTIONS = {
    ShortformAction.ASK,
    ShortformAction.SAVE_AND_ASK,
    ShortformAction.CLARIFY,
    ShortformAction.SUGGEST_SWITCH,
    ShortformAction.RESOLVE_CONFLICT,
    ShortformAction.OUT_OF_SCOPE,
}
_REMOVED_CATEGORY_LABELS = {
    "사람·브랜드 이야기",
    "사람/브랜드 이야기",
    "이용 정보",
    "이용정보",
    "후기·신뢰·전문성",
    "후기/신뢰/전문성",
}


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

    def create_session(
        self, db: Session, store_context: StoreContext
    ) -> ShortformSessionCreateResponse:
        stored_context = enrich_store_context_with_trade_area(
            db,
            store_context.model_dump(mode="json"),
        )
        session = ShortformSession(
            id=f"sf_{uuid.uuid4().hex}",
            status=ShortformSessionStatus.COLLECTING.value,
            store_id=store_context.store.store_id,
            store_context=stored_context,
            project_state=_initial_project_state(),
            conversation=[],
            shown_video_editing_db_ids=[],
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

        entry_response = self._handle_entry_mode_option(db, session, turn_input)
        if entry_response is not None:
            return entry_response

        project_state = _apply_deterministic_turn_input(
            dict(session.project_state or {}),
            turn_input,
            session.store_context or {},
        )
        selected_category = _promotion_category_from_option(turn_input)
        if selected_category is not None:
            project_state["promotion_category"] = selected_category.value
        session.project_state = project_state

        user_input = turn_input.model_dump(mode="json", exclude_none=True)
        if turn_input.type == TurnInputType.OPTION:
            option_labels = project_state.get("option_labels") or {}
            option_label = option_labels.get(str(turn_input.option_id or ""))
            if option_label:
                user_input["option_label"] = option_label
        if project_state.get("current_question"):
            user_input["answering_question"] = project_state["current_question"]

        graph_result = self._invoke_graph(
            {
                "mode": "TURN",
                "domain_context": self.domain_context,
                "store_context": session.store_context,
                "project_state": project_state,
                "conversation": session.conversation,
                "user_input": user_input,
                "photo_urls": self._photo_urls(session.store_context),
            },
            correlation_id=session.id,
        )
        decision = ShortformTurnDecision.model_validate(graph_result["decision"])
        project_state = self._merge_project_state(project_state, decision)

        # Code, not the LLM, is authoritative for recommendation readiness.
        missing = _missing_required_fields(project_state)
        project_state["missing_required_fields"] = missing
        project_state["store_context_conflicts"] = [
            item.model_dump(mode="json") for item in decision.conflicts
        ]
        project_state["ready_for_confirmation"] = not missing and not decision.conflicts

        action = decision.action
        removed_objective_question = _asks_removed_promotion_objective(decision)
        if action == ShortformAction.RECOMMEND and not project_state.get("brief_confirmed"):
            action = (
                ShortformAction.CONFIRM
                if project_state["ready_for_confirmation"]
                else ShortformAction.ASK
            )
        if action == ShortformAction.CONFIRM and not project_state["ready_for_confirmation"]:
            action = ShortformAction.ASK
        if removed_objective_question:
            action = (
                ShortformAction.CONFIRM
                if project_state["ready_for_confirmation"]
                else ShortformAction.ASK
            )

        previous_question = str(session.project_state.get("current_question") or "").strip()
        next_question_field = _infer_question_field(decision.options, missing)
        if action == ShortformAction.CONFIRM:
            assistant_message = _confirmation_message(project_state)
            decision = decision.model_copy(update={"options": []})
        elif removed_objective_question:
            assistant_message, fallback_options = _fallback_question(next_question_field)
            decision = decision.model_copy(update={"options": fallback_options})
        else:
            assistant_message = _format_assistant_message(decision.assistant_message, action)
            extracted_question = _extract_question(assistant_message)
            if action in _QUESTION_ACTIONS and next_question_field and not extracted_question:
                fallback_question, fallback_options = _fallback_question(next_question_field)
                assistant_message = _format_assistant_message(
                    f"{assistant_message}\n{fallback_question}",
                    action,
                )
                if fallback_options:
                    decision = decision.model_copy(update={"options": fallback_options})
            elif action in _QUESTION_ACTIONS and previous_question == extracted_question:
                assistant_message, fallback_options = _fallback_question(next_question_field)
                if fallback_options:
                    decision = decision.model_copy(update={"options": fallback_options})
        project_state["current_question"] = (
            _extract_question(assistant_message) if action in _QUESTION_ACTIONS else None
        )
        project_state["current_question_field"] = (
            next_question_field if action in _QUESTION_ACTIONS else None
        )
        project_state["ready_for_recommendation"] = bool(
            project_state.get("brief_confirmed") and project_state.get("ready_for_confirmation")
        )

        session.project_state = project_state
        session.status = (
            ShortformSessionStatus.CONFIRMING.value
            if action == ShortformAction.CONFIRM
            else ShortformSessionStatus.COLLECTING.value
        )
        session.conversation = _append_conversation(
            session.conversation,
            _turn_input_to_text(turn_input),
            assistant_message,
        )
        db.commit()

        if action == ShortformAction.RECOMMEND and project_state.get("brief_confirmed"):
            return self._recommend(db, session)

        options = _sanitize_options(decision.options, action)
        project_state["option_labels"] = {item.id: item.label for item in options}
        session.project_state = project_state
        db.commit()
        return ShortformTurnResponse(
            session_id=session.id,
            action=action,
            assistant_message=assistant_message,
            project_state=ShortformProjectState.model_validate(project_state),
            options=options,
            recommendations=[],
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
            # The contract guarantees a recommendation, so still serve one, but a
            # "next" call before any first recommendation means the caller skipped
            # the confirmation turn's RECOMMEND response — surface that ordering
            # anomaly instead of masking it.
            logger.warning(
                "next_recommendation called for session %s before any current "
                "recommendation exists; check the backend call ordering.",
                session.id,
            )
        response = self._recommend(
            db,
            session,
            user_event="[UI_EVENT] 다시 추천 받기",
        )
        assert response.recommendations
        return NextRecommendationResponse(
            session_id=session.id,
            recommendations=response.recommendations,
            shown_template_ids=list(session.shown_video_editing_db_ids or []),
        )

    def delete_session(self, db: Session, session_id: str) -> None:
        session = self._get_session(db, session_id)
        db.delete(session)
        db.commit()

    def _handle_entry_mode_option(
        self,
        db: Session,
        session: ShortformSession,
        turn_input: ShortformTurnInput,
    ) -> ShortformTurnResponse | None:
        if turn_input.type != TurnInputType.OPTION:
            return None
        if session.project_state.get("entry_mode"):
            return None

        option_id = str(turn_input.option_id or "").upper()
        if option_id == "PROMOTION_GUIDE":
            entry_mode = ShortformEntryMode.PROMOTION_GUIDE
            message = "무엇을 홍보하고 싶으세요?"
            options = _promotion_category_options()
        elif option_id == "FREE_INPUT":
            entry_mode = ShortformEntryMode.FREE_INPUT
            message = (
                "어떤 영상을 만들고 싶은지 편하게 말씀해주세요. 필요한 정보만 하나씩 확인할게요."
            )
            options = []
        else:
            return None

        project_state = dict(session.project_state or {})
        project_state["entry_mode"] = entry_mode.value
        project_state["current_question"] = _extract_question(message)
        project_state["current_question_field"] = (
            "promotion_subject" if option_id == "PROMOTION_GUIDE" else None
        )
        project_state["option_labels"] = {item.id: item.label for item in options}
        project_state["ready_for_recommendation"] = False
        session.project_state = project_state
        session.status = ShortformSessionStatus.COLLECTING.value
        session.conversation = _append_conversation(
            session.conversation,
            _turn_input_to_text(turn_input),
            message,
        )
        db.commit()
        return ShortformTurnResponse(
            session_id=session.id,
            action=ShortformAction.ASK,
            assistant_message=message,
            project_state=ShortformProjectState.model_validate(project_state),
            options=options,
            recommendations=[],
        )

    def get_shooting_guide(
        self,
        db: Session,
        template_id: str,
        version: int,
        context: dict[str, str | None] | None = None,
    ) -> ShootingGuideResponse:
        template = db.get(VideoEditingDBRecord, (template_id, version))
        if template is None:
            active_templates = db.scalars(
                select(VideoEditingDBRecord)
                .where(VideoEditingDBRecord.status == "ACTIVE")
                .order_by(VideoEditingDBRecord.version.desc())
            )
            template = next(
                (row for row in active_templates if template_id in (row.trend_ids or [])),
                None,
            )
        if template is None:
            raise ShortformDomainError(
                "EDITING_TEMPLATE_NOT_FOUND",
                "Editing template was not found.",
                status_code=404,
            )
        normalized_context = {
            key: str(value).strip()
            for key, value in (context or {}).items()
            if value is not None and str(value).strip()
        }
        guide = _personalize_guide_value(dict(template.shooting_guide or {}), normalized_context)
        format_type = str((template.recommendation_metadata or {}).get("format_type") or "밈")
        scenes = []
        for item in guide.get("scenes") or []:
            scene = dict(item)
            scene["scene_dialogue"] = str(scene.get("scene_dialogue") or "")
            if len(scene["scene_dialogue"]) > 9:
                raise ShortformDomainError(
                    "SHOOTING_GUIDE_DIALOGUE_TOO_LONG",
                    "scene_dialogue must be at most 9 characters including spaces.",
                    status_code=422,
                )
            scene["scene_subtitle"] = str(scene.get("scene_subtitle") or "")
            scenes.append(scene)

        tasks = []
        for index, item in enumerate(guide.get("tasks") or [], start=1):
            task = dict(item)
            display_order = int(task.get("display_order") or task.get("task_order") or index)
            scene_index = task.get("scene_index")
            if scene_index is None and task.get("shooting_scene_order") is not None:
                scene_index = int(task["shooting_scene_order"]) - 1
            if scene_index is None:
                scene_index = display_order - 1

            # Legacy records used the full instruction in `description`. Keep that
            # text intact below as an instruction instead of clipping it into a title.
            task_title = _validated_guide_title(
                task.get("task_title") or task.get("title"), "촬영 태스크"
            )
            raw_guide = task.get("guide") if isinstance(task.get("guide"), dict) else {}
            raw_instructions = raw_guide.get("instructions") or []
            if isinstance(raw_instructions, str):
                raw_instructions = [raw_instructions]
            instructions = [
                str(value).strip()[:500] for value in raw_instructions if str(value).strip()
            ]
            legacy_description = str(task.get("description") or "").strip()
            if not instructions and legacy_description:
                instructions = [legacy_description[:500]]

            start_ms, end_ms = _shooting_task_interval_ms(
                task=task,
                scenes=scenes,
                scene_index=int(scene_index),
                display_order=display_order,
                evidence_summary=template.evidence_summary or {},
            )

            tasks.append(
                {
                    "display_order": display_order,
                    "task_title": task_title,
                    "scene_index": int(scene_index),
                    "guide": {
                        "instructions": instructions,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                    },
                }
            )

        # 2026-08-30 이전에는 컷 개수·복잡도와 무관하게 "완성 길이×10초"로만
        # 근사했다 — 같은 완성 길이라도 컷이 4개인 영상과 20개인 영상의 실제
        # 촬영 시간이 같을 리 없다는 문제가 있었다. 지금은 Gemini가 최초 분석
        # 시점에 분류한 촬영 시간 버킷(`minimum_filming_time`, `filming_time`과
        # 값 집합이 같다)을 기존 초 단위 API 계약으로 환산해 내려준다.
        # 버킷이 없는 구버전 템플릿은 예전 근사식으로 초를 계산한 뒤
        # 같은 버킷으로 눌러 담고, 그 버킷의 초 값을 응답한다.
        shooting_time_bucket = (template.recommendation_metadata or {}).get("minimum_filming_time")
        if shooting_time_bucket not in FILMING_TIME_BUCKET_SECONDS:
            final_duration = sum(
                max(int(scene.get("target_duration_sec") or 0), 0) for scene in scenes
            )
            legacy_sec = (
                max(final_duration * 10, 60)
                if final_duration
                else max(int(guide.get("estimated_shooting_sec") or 60), 60)
            )
            shooting_time_bucket = _filming_time_bucket_from_seconds(legacy_sec)
        estimated_shooting_sec = FILMING_TIME_BUCKET_SECONDS[shooting_time_bucket]

        return ShootingGuideResponse(
            template_id=template.template_id,
            version=template.version,
            estimated_shooting_sec=estimated_shooting_sec,
            estimated_shooting_time_bucket=shooting_time_bucket,
            required_people=max(int(guide.get("required_people") or 1), 1),
            props=[str(item) for item in (guide.get("props") or []) if str(item).strip()],
            difficulty=str(
                guide.get("difficulty")
                or template.recommendation_metadata.get("difficulty")
                or "중"
            ),
            format_type=format_type,
            scenes=scenes,
            tasks=tasks,
            context_applied=normalized_context,
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
        project_state["ready_for_recommendation"] = True
        project_state["current_question"] = None
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
        project_state["ready_for_recommendation"] = False
        message = "바뀐 항목만 반영할게요. 어떤 내용을 수정할까요?"
        project_state["current_question"] = _extract_question(message)
        session.project_state = project_state
        session.status = ShortformSessionStatus.COLLECTING.value
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
            recommendations=[],
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
            if user_event:
                raise ShortformDomainError(
                    "NO_MORE_SHORTFORM_RECOMMENDATIONS",
                    "All available video-editing DB templates have already been shown.",
                    status_code=409,
                    retryable=False,
                )
            raise ShortformDomainError(
                "NO_ACTIVE_VIDEO_EDITING_DB",
                "The packaged video-editing DB could not provide an ACTIVE record.",
                status_code=503,
                retryable=True,
            )

        selected_items = self._select_video_editing_dbs(session, candidates)
        recommendations: list[ShortformRecommendation] = []
        stored_items: list[dict[str, Any]] = []
        for selection, selected in selected_items:
            recommendation = ShortformRecommendation(
                recommendation_id=f"rec_{uuid.uuid4().hex}",
                project_title=(
                    selection.project_title or selected.recommendation_title or selected.name
                ).strip(),
                # The recommendation card title is authoritative catalog data.
                title=selected.name.strip(),
                concept=(
                    selection.concept or selected.recommendation_concept or selected.name
                ).strip(),
                editing_template_id=selected.video_editing_db_id,
                editing_template_version=selected.video_editing_db_version,
                reference_url=selected.reference_url,
                guide_video_url=selected.guide_video_url,
                source_platform=selected.source_platform,
            )
            stored = recommendation.model_dump(mode="json")
            stored["internal_reason"] = selection.internal_reason
            recommendations.append(recommendation)
            stored_items.append(stored)

        session.current_recommendation = {"recommendations": stored_items}
        session.status = ShortformSessionStatus.WAITING_RECOMMENDATION_ACTION.value
        shown = list(session.shown_video_editing_db_ids or [])
        for _, selected in selected_items:
            if selected.video_editing_db_id not in shown:
                shown.append(selected.video_editing_db_id)
        session.shown_video_editing_db_ids = shown
        conversation = list(session.conversation or [])
        if user_event:
            conversation.append({"role": "user", "content": user_event})
        conversation.append(
            {
                "role": "assistant",
                "content": "[RECOMMENDATIONS] "
                + " | ".join(f"{item.title} — {item.concept}" for item in recommendations),
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
            recommendations=recommendations,
        )

    def _recommendation_candidates(
        self,
        db: Session,
        session: ShortformSession,
    ) -> list[VideoEditingDBCandidate]:
        """Return the best non-empty pool, capped to three recommendations later.

        Relevance is a preference, never an availability gate: prefer a tier that
        can fill three cards, otherwise use the largest non-empty tier so a catalog
        with one or two retained templates remains usable. Only a fully empty pool
        makes `next` report NO_MORE_SHORTFORM_RECOMMENDATIONS.
        """

        largest_pool: list[VideoEditingDBCandidate] = []
        for mode in ("strict", "safe", "any"):
            candidates = self._video_editing_db_candidates(
                db,
                session,
                constraint_mode=mode,
                exclude_shown=True,
            )
            if len(candidates) >= 3:
                return candidates
            if len(candidates) > len(largest_pool):
                largest_pool = candidates

        return largest_pool

    def _select_video_editing_dbs(
        self,
        session: ShortformSession,
        candidates: list[VideoEditingDBCandidate],
    ) -> list[tuple[VideoEditingDBSelection, VideoEditingDBCandidate]]:
        """Select up to three distinct candidates in one LLM call, with a stable fallback."""

        if len(candidates) < 3:
            # The selection schema requires exactly three distinct keys, which a
            # smaller pool can never satisfy; recommend the remaining records
            # deterministically instead of burning an LLM call that must fail.
            return self._fallback_selections(candidates)
        try:
            graph_result = self._invoke_graph(
                {
                    "mode": "RECOMMEND",
                    "domain_context": self.domain_context,
                    "store_context": session.store_context,
                    "project_state": session.project_state,
                    "conversation": session.conversation,
                    "video_editing_db_candidates": [
                        item.model_dump(mode="json") for item in candidates
                    ],
                },
                correlation_id=session.id,
            )
            selections = VideoEditingDBSelections.model_validate(
                graph_result["recommendations"]
            ).selections
            by_key = {item.candidate_key: item for item in candidates}
            result = []
            for selection in selections:
                selected = by_key.get(selection.candidate_key)
                if selected is None:
                    raise ValueError("selection is outside the candidate pool")
                result.append((selection, selected))
            return result
        except (
            ShortformLLMError,
            ShortformDomainError,
            ValidationError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            # Conversation still requires the LLM, but once a brief is confirmed a
            # recommendation must not disappear because the contextual selector is
            # temporarily unavailable or returns malformed structured output.
            logger.warning(
                "Shortform recommendation selector failed; using stable fallback candidates (%s)",
                type(exc).__name__,
            )
            return self._fallback_selections(candidates)

    @staticmethod
    def _fallback_selections(
        candidates: list[VideoEditingDBCandidate],
    ) -> list[tuple[VideoEditingDBSelection, VideoEditingDBCandidate]]:
        return [
            (
                VideoEditingDBSelection(
                    candidate_key=selected.candidate_key,
                    project_title=(f"{selected.recommendation_title or selected.name} 프로젝트"),
                    title=selected.name,
                    concept=selected.recommendation_concept or selected.name,
                    internal_reason=(
                        "Deterministic availability fallback after contextual "
                        "recommendation selection failed."
                    ),
                ),
                selected,
            )
            for selected in candidates[:3]
        ]

    def _video_editing_db_candidates(
        self,
        db: Session,
        session: ShortformSession,
        *,
        constraint_mode: _CandidateConstraintMode,
        exclude_shown: bool,
    ) -> list[VideoEditingDBCandidate]:
        rows = self._active_video_editing_db_rows(db)
        latest_by_id: dict[str, VideoEditingDBRecord] = {}
        for row in rows:
            latest_by_id.setdefault(row.template_id, row)

        project_state = session.project_state or {}
        shown = set(session.shown_video_editing_db_ids or [])
        result: list[VideoEditingDBCandidate] = []
        for template in latest_by_id.values():
            if exclude_shown and template.template_id in shown:
                continue
            metadata = dict(template.recommendation_metadata or {})
            if constraint_mode != "any" and not _passes_hard_constraints(metadata, project_state):
                continue
            if constraint_mode == "strict" and not _passes_soft_constraints(
                metadata, project_state
            ):
                continue
            trend_context = self._trend_context(db, template.trend_ids or [])
            playable_trend = next(
                (
                    item
                    for item in trend_context
                    if _is_playable_youtube_url(item.get("representative_youtube_url"))
                    and _is_playable_youtube_url(item.get("guide_youtube_url"))
                ),
                None,
            )
            if playable_trend is None:
                continue
            result.append(
                VideoEditingDBCandidate(
                    candidate_key=f"{template.template_id}@{template.version}",
                    video_editing_db_id=template.template_id,
                    video_editing_db_version=template.version,
                    name=template.name,
                    recommendation_title=template.recommendation_title or template.name,
                    recommendation_concept=template.recommendation_concept or template.name,
                    recommendation_metadata=metadata,
                    trend_context=trend_context,
                    reference_url=str(playable_trend["representative_youtube_url"]),
                    guide_video_url=str(playable_trend["guide_youtube_url"]),
                )
            )
        return result

    @staticmethod
    def _active_video_editing_db_rows(db: Session) -> list[VideoEditingDBRecord]:
        def load() -> list[VideoEditingDBRecord]:
            return list(
                db.scalars(
                    select(VideoEditingDBRecord)
                    .where(VideoEditingDBRecord.status == "ACTIVE")
                    .order_by(
                        VideoEditingDBRecord.template_id.asc(),
                        VideoEditingDBRecord.version.desc(),
                    )
                )
            )

        rows = load()
        if rows:
            return rows

        # Production startup can legitimately reach the first recommendation before
        # the explicit bootstrap endpoint is called. Recover from that deployment
        # ordering by importing the packaged, validated three-record DB idempotently.
        seed_template_library(db)
        return load()

    def _trend_context(self, db: Session, trend_ids: list[str]) -> list[dict[str, Any]]:
        if not trend_ids:
            return []
        rows = list(db.scalars(select(Challenge).where(Challenge.id.in_(trend_ids))))
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
                "guide_youtube_url": (
                    row.override_guide_youtube_url
                    if row.guide_video_overridden
                    else row.automatic_guide_youtube_url
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
            state["promotion_category"] = updates.promotion_category.value
        if updates.promotion_subject is not None:
            state["promotion_subject"] = {
                "type": updates.promotion_subject.type,
                "name": updates.promotion_subject.name,
                "menu_id": updates.promotion_subject.menu_id,
                "details": {item.key: item.value for item in updates.promotion_subject.details},
            }
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
        photos = (store_context.get("store") or {}).get("store_photos") or []
        urls = [
            str(item.get("asset_url") or "").strip() for item in photos if isinstance(item, dict)
        ]
        return [url for url in urls if url][: self.settings.shortform_max_photo_inputs]

    def _invoke_graph(
        self,
        state: dict[str, Any],
        *,
        correlation_id: str,
    ) -> dict[str, Any]:
        try:
            operation = {
                "TURN": "turn",
                "RECOMMEND": "recommend",
            }.get(str(state.get("mode") or ""))
            if operation is None:
                raise ValueError("Unsupported Shortform graph mode")
            return shortform_harness.execute(
                operation=operation,
                input_value=state,
                executor=self.graph.invoke,
                correlation_id=correlation_id,
                repair_executor=lambda value, _result, _issues, _attempt: self.graph.invoke(
                    value
                ),
            )
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
        state = dict(session.project_state or {})
        state.pop("promotion_objective", None)
        if state.get("current_question_field") == "promotion_objective":
            state["current_question"] = None
            state["current_question_field"] = None
            state["option_labels"] = {}
        category = state.get("promotion_category")
        if category not in {item.value for item in PromotionCategory}:
            state["promotion_category"] = None
        session.project_state = state
        return session


_SCENE_INTERVAL_PATTERN = re.compile(
    r"(?P<start>\d+(?:\.\d+)?)\s*(?:~|[-–—])\s*"
    r"(?P<end>\d+(?:\.\d+)?)\s*초"
)
_SEGMENT_INTERVAL_PATTERN = re.compile(
    r"start=(?P<start>\d+(?:\.\d+)?)s\|end=(?P<end>\d+(?:\.\d+)?)s"
)


def _shooting_task_interval_ms(
    *,
    task: dict[str, Any],
    scenes: list[dict[str, Any]],
    scene_index: int,
    display_order: int,
    evidence_summary: dict[str, Any],
) -> tuple[int, int]:
    """Resolve an absolute reference-video interval for one shooting task."""
    raw_guide = task.get("guide") if isinstance(task.get("guide"), dict) else {}
    direct = _valid_interval_ms(raw_guide.get("start_ms"), raw_guide.get("end_ms"))
    if direct is not None:
        return direct

    scene = scenes[scene_index] if 0 <= scene_index < len(scenes) else {}
    scene_role = str(scene.get("scene_role") or "").strip()
    evidence = _evidence_interval_ms(
        evidence_summary,
        display_order=display_order,
        scene_role=scene_role,
    )
    if evidence is not None:
        return evidence

    description = str(scene.get("scene_description") or "")
    for pattern in (_SEGMENT_INTERVAL_PATTERN, _SCENE_INTERVAL_PATTERN):
        match = pattern.search(description)
        if match is not None:
            parsed = _seconds_interval_ms(match.group("start"), match.group("end"))
            if parsed is not None:
                return parsed

    cursor_ms = 0
    for index, candidate in enumerate(scenes):
        duration_ms = max(
            1,
            int(round(float(candidate.get("target_duration_sec") or 0) * 1000)),
        )
        if index == scene_index:
            return cursor_ms, cursor_ms + duration_ms
        cursor_ms += duration_ms

    # Legacy-corrupt data may contain a task without a matching scene. Keep the
    # response contract deterministic while exposing a visibly synthetic range.
    return max(0, display_order - 1) * 1000, max(1, display_order) * 1000


def _evidence_interval_ms(
    evidence_summary: dict[str, Any],
    *,
    display_order: int,
    scene_role: str,
) -> tuple[int, int] | None:
    insights = evidence_summary.get("video_insights")
    if not isinstance(insights, list):
        return None
    for insight in insights:
        if not isinstance(insight, dict):
            continue
        segments = insight.get("segments")
        if not isinstance(segments, list):
            continue
        ordered_match: dict[str, Any] | None = None
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            if int(segment.get("sequence") or 0) == display_order:
                ordered_match = segment
            if scene_role and str(segment.get("scene_role") or "").strip() == scene_role:
                parsed = _seconds_interval_ms(
                    segment.get("start_sec"),
                    segment.get("end_sec"),
                )
                if parsed is not None:
                    return parsed
        if ordered_match is not None:
            parsed = _seconds_interval_ms(
                ordered_match.get("start_sec"),
                ordered_match.get("end_sec"),
            )
            if parsed is not None:
                return parsed
    return None


def _seconds_interval_ms(start: Any, end: Any) -> tuple[int, int] | None:
    try:
        start_ms = int(round(float(start) * 1000))
        end_ms = int(round(float(end) * 1000))
    except (TypeError, ValueError):
        return None
    return _valid_interval_ms(start_ms, end_ms)


def _valid_interval_ms(start: Any, end: Any) -> tuple[int, int] | None:
    try:
        start_ms = int(start)
        end_ms = int(end)
    except (TypeError, ValueError):
        return None
    if start_ms < 0 or end_ms <= start_ms:
        return None
    return start_ms, end_ms


def _initial_project_state() -> dict[str, Any]:
    return ShortformProjectState(
        missing_required_fields=list(_REQUIRED_FIELDS),
        current_question="오늘 어떤 영상을 찍을까요?",
        ready_for_confirmation=False,
        ready_for_recommendation=False,
        brief_confirmed=False,
    ).model_dump(mode="json")


def _missing_required_fields(state: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    subject = state.get("promotion_subject")
    if not isinstance(subject, dict) or not str(subject.get("name") or "").strip():
        missing.append("promotion_subject")
    for field in ("filming_time", "face_exposure"):
        if not state.get(field):
            missing.append(field)
    return missing


def _validated_guide_title(value: Any, fallback: str) -> str:
    title = str(value or fallback).strip() or fallback
    if len(title) > MAX_SHOOTING_GUIDE_TITLE_CHARS:
        raise ShortformDomainError(
            "SHOOTING_GUIDE_TITLE_TOO_LONG",
            (f"촬영 컷 설명은 공백 포함 {MAX_SHOOTING_GUIDE_TITLE_CHARS}자 이하여야 합니다."),
            status_code=422,
        )
    return title


def _apply_deterministic_turn_input(
    state: dict[str, Any],
    turn_input: ShortformTurnInput,
    store_context: dict[str, Any],
) -> dict[str, Any]:
    """Persist canonical UI answers before asking the LLM to interpret free text.

    The LLM still handles natural conversation, but stable option IDs must never be
    lost because of a transient structured-output omission. This is also what keeps
    an already answered question from being asked again.
    """

    result = dict(state)
    if turn_input.type == TurnInputType.OPTION:
        raw_id = str(turn_input.option_id or "").strip()
        option_id = raw_id.lower()
        if option_id in {item.value for item in FilmingTime}:
            result["filming_time"] = option_id
        elif option_id in {item.value for item in FaceExposure}:
            result["face_exposure"] = option_id
        else:
            for menu in store_context.get("representative_menus") or []:
                if str(menu.get("menu_id") or "") == raw_id:
                    result["promotion_subject"] = {
                        "type": "MENU",
                        "name": str(menu.get("name") or "").strip(),
                        "menu_id": raw_id,
                        "details": {},
                    }
                    break
        return result

    if turn_input.type != TurnInputType.TEXT:
        return result
    text = str(turn_input.text or "").strip()
    field = result.get("current_question_field")
    if field == "promotion_subject":
        category = str(result.get("promotion_category") or "").upper() or None
        result["promotion_subject"] = {
            "type": category,
            "name": text,
            "menu_id": None,
            "details": {},
        }
    elif field == "filming_time":
        filming_time = _filming_time_from_text(text)
        if filming_time:
            result["filming_time"] = filming_time
    elif field == "face_exposure":
        face_exposure = _face_exposure_from_text(text)
        if face_exposure:
            result["face_exposure"] = face_exposure
    return result


def _filming_time_bucket_from_minutes(minutes: float) -> str:
    """분 단위 촬영 시간을 표준 버킷으로 분류한다.

    사용자 응답 해석(`_filming_time_from_text`)과 구버전 템플릿의 초 단위 근사치
    변환(`_filming_time_bucket_from_seconds`)이 이 경계를 공유한다 — 두 곳이
    따로 하드코딩되어 어긋나면, 같은 시간이 사용자 응답에서는 within_10m으로,
    템플릿에서는 within_20m으로 분류되어 추천 필터가 어긋난다.
    """
    if minutes <= 5:
        return FilmingTime.WITHIN_5M.value
    if minutes <= 10:
        return FilmingTime.WITHIN_10M.value
    if minutes <= 20:
        return FilmingTime.WITHIN_20M.value
    return FilmingTime.PLUS_30M.value


def _filming_time_from_text(text: str) -> str | None:
    match = re.search(r"(\d+)\s*분", text)
    if not match:
        return None
    return _filming_time_bucket_from_minutes(int(match.group(1)))


def _filming_time_bucket_from_seconds(seconds: int) -> str:
    """구버전 템플릿(버킷 미분류)의 초 단위 근사치를 표준 버킷으로 눌러 담는다."""
    return _filming_time_bucket_from_minutes(max(seconds, 1) / 60)


def _face_exposure_from_text(text: str) -> str | None:
    normalized = text.replace(" ", "")
    if any(token in normalized for token in ("안돼", "안됨", "싫", "노출없", "얼굴없이")):
        return FaceExposure.NOT_ALLOWED.value
    if any(token in normalized for token in ("괜찮", "가능", "나와도", "노출해도")):
        return FaceExposure.ALLOWED.value
    return None


def _infer_question_field(options: list[Any], missing: list[str]) -> str | None:
    option_ids = {str(item.id).strip().lower() for item in options}
    if option_ids and option_ids <= {item.value for item in FilmingTime}:
        return "filming_time"
    if option_ids and option_ids <= {item.value for item in FaceExposure}:
        return "face_exposure"
    return missing[0] if missing else None


def _asks_removed_promotion_objective(decision: ShortformTurnDecision) -> bool:
    if decision.action not in _QUESTION_ACTIONS:
        return False
    option_ids = {str(item.id).strip().lower() for item in decision.options}
    objective_ids = {item.value for item in PromotionObjective}
    if option_ids & objective_ids:
        return True
    message = decision.assistant_message.replace(" ", "").lower()
    return any(
        marker in message
        for marker in (
            "홍보목적",
            "어떤결과를",
            "가장원하세요",
            "판매를늘",
            "판매늘",
            "매출",
            "인지도",
            "방문유도",
            "promotionobjective",
        )
    )


def _confirmation_message(state: dict[str, Any]) -> str:
    subject = state.get("promotion_subject") or {}
    subject_name = str(subject.get("name") or "선택한 대상").strip()
    filming_label = {
        FilmingTime.WITHIN_5M.value: "5분 이내",
        FilmingTime.WITHIN_10M.value: "10분 이내",
        FilmingTime.WITHIN_20M.value: "20분 이내",
        FilmingTime.PLUS_30M.value: "30분 이상",
    }.get(str(state.get("filming_time") or ""), "입력한 시간")
    face_label = {
        FaceExposure.ALLOWED.value: "얼굴 노출 가능",
        FaceExposure.NOT_ALLOWED.value: "얼굴 노출 없이",
    }.get(str(state.get("face_exposure") or ""), "입력한 얼굴 노출 방식")
    return (
        f"{subject_name} 홍보 영상으로 준비할게요. 촬영 시간은 {filming_label}, "
        f"{face_label}로 이해했어요. 이대로 추천받을까요?"
    )


def _fallback_question(field: str | None) -> tuple[str, list[DecisionOption]]:
    if field == "filming_time":
        return (
            "촬영에 어느 정도 시간을 쓸 수 있으세요?",
            [
                DecisionOption(id="within_5m", label="5분 이내"),
                DecisionOption(id="within_10m", label="10분 이내"),
                DecisionOption(id="within_20m", label="20분 이내"),
                DecisionOption(id="30m_plus", label="30분 이상"),
            ],
        )
    if field == "face_exposure":
        return (
            "영상에 얼굴이 나와도 괜찮으세요?",
            [
                DecisionOption(id="allowed", label="얼굴 노출 가능"),
                DecisionOption(id="not_allowed", label="얼굴 노출 없이"),
            ],
        )
    return "무엇을 홍보하고 싶으세요?", []


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


def _promotion_category_options() -> list[ShortformOption]:
    return [
        ShortformOption(id="MENU", label="메뉴"),
        ShortformOption(id="SPACE", label="가게 공간·분위기"),
        ShortformOption(id="EVENT", label="이벤트·혜택·할인"),
    ]


def _promotion_category_from_option(
    turn_input: ShortformTurnInput,
) -> PromotionCategory | None:
    if turn_input.type != TurnInputType.OPTION:
        return None
    mapping = {
        "MENU": PromotionCategory.MENU,
        "SPACE": PromotionCategory.SPACE,
        "EVENT": PromotionCategory.EVENT,
    }
    return mapping.get(str(turn_input.option_id or "").upper())


def _sanitize_options(
    options: list[Any],
    action: ShortformAction,
) -> list[ShortformOption]:
    if action == ShortformAction.SUGGEST_SWITCH:
        return _promotion_category_options()
    result: list[ShortformOption] = []
    for item in options:
        option_id = str(item.id).strip()
        label = str(item.label).strip()
        if (
            option_id.lower() in _REMOVED_PROMOTION_OBJECTIVE_OPTION_IDS
            or label in _REMOVED_CATEGORY_LABELS
        ):
            continue
        if option_id and label:
            result.append(ShortformOption(id=option_id, label=label))
    return result


def _format_assistant_message(message: str, action: ShortformAction) -> str:
    if action in _QUESTION_ACTIONS:
        return _single_question_message(message)
    return _limit_message_length(message.strip(), 500)


def _single_question_message(message: str) -> str:
    if not message:
        return ""

    if not message.strip():
        return ""

    clean_lines = [
        re.sub(r"^\s*[\*\-\d]+\s*[)\.]\s*", "", part.strip())
        for part in message.strip().splitlines()
        if part.strip()
    ]
    if clean_lines:
        candidate = " ".join(clean_lines)
    else:
        candidate = message.strip()
    candidate = re.sub(r"\s+", " ", candidate)

    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", candidate) if part.strip()]
    if not sentences:
        sentences = [candidate]

    summary = sentences[0].rstrip()
    question = next((item for item in sentences if item.endswith("?")), None)

    if question:
        if question == summary:
            return _limit_message_length(question, 240)
        return _limit_message_length(f"{summary}\n{question}", 260)

    # If the model did not include a question mark, keep the shortest meaningful
    # one-line summary and keep the fallback concise.
    return _limit_message_length(summary, 180)


def _extract_question(message: str) -> str | None:
    if not message:
        return None
    parts = [part.strip() for part in re.split(r"(?<=[?])\s+", message.strip()) if part.strip()]
    return next((part for part in parts if part.endswith("?")), None)


def _limit_message_length(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _turn_input_to_text(turn_input: ShortformTurnInput) -> str:
    if turn_input.type == TurnInputType.TEXT:
        return turn_input.text or ""
    if turn_input.type == TurnInputType.OPTION:
        return f"[OPTION] {turn_input.option_id}"
    return f"[CONFIRM] {turn_input.value}"


def _personalize_guide_value(value: Any, context: dict[str, str]) -> Any:
    """Apply only explicit placeholders from the persisted guide template."""
    if isinstance(value, str):
        personalized = value
        for key, replacement in context.items():
            personalized = personalized.replace("{" + key + "}", replacement)
        return personalized
    if isinstance(value, list):
        return [_personalize_guide_value(item, context) for item in value]
    if isinstance(value, dict):
        return {key: _personalize_guide_value(item, context) for key, item in value.items()}
    return value


def _is_playable_youtube_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (
        host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
        or host.endswith(".youtube.com")
    )


@lru_cache(maxsize=1)
def _load_domain_context() -> str:
    return Path(__file__).with_name("context.md").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_shortform_agent_service() -> ShortformAgentService:
    return ShortformAgentService()
