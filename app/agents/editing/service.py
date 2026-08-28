from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.editing.effect_planner import EffectPlanner
from app.agents.editing.graph import build_editing_graph
from app.agents.editing.llm import EditingLLM, OpenAIEditingLLM
from app.agents.editing.structured_output import EditingLLMError
from app.agents.editing.telemetry import reset_usage, usage_snapshot
from app.agents.editing.renderer import EditingRenderer, HttpEditingRenderer
from app.agents.editing.reals import RealsRegistryError, get_reals_registry
from app.agents.editing.types import (
    EditingPlanDecision,
    persistable_video_context,
)
from app.agents.editing.validator import EditRecipeValidator
from app.agents.editing.video_context import FFmpegVideoContextBuilder, VideoContextBuilder
from app.core.config import get_settings
from app.models.editing_run import EditingRun
from app.models.shortform_session import ShortformSession
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.schemas.editing import (
    EditRecipe,
    EditingRevisionRequest,
    EditingRevisionResponse,
    EditingRunCreateRequest,
    EditingRunCreateResponse,
    EditingRunResultResponse,
    EditingRunStage,
    EditingRunStatus,
    PublishingResult,
    PublishingTrack,
    RecipeCaption,
    RecipeClip,
    RecipeCta,
    SelectedShortform,
)


class EditingDomainError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class EditingAgentService:
    def __init__(
        self,
        *,
        llm: EditingLLM | None = None,
        video_context_builder: VideoContextBuilder | None = None,
        validator: EditRecipeValidator | None = None,
        renderer: EditingRenderer | None = None,
        effect_planner: EffectPlanner | None = None,
    ) -> None:
        self.llm = llm or OpenAIEditingLLM()
        self.video_context_builder = video_context_builder or FFmpegVideoContextBuilder()
        self.validator = validator or EditRecipeValidator()
        self.renderer = renderer or HttpEditingRenderer()
        self.effect_planner = effect_planner or EffectPlanner()
        self.settings = get_settings()
        self.domain_context = _load_domain_context()
        self.graph = build_editing_graph(self.llm, self.validator)

    def create_run(self, db: Session, request: EditingRunCreateRequest) -> EditingRun:
        self._validate_video_limit(request.videos)
        database = self._get_active_database(db, request.selected_shortform)
        request.videos = _normalize_video_inputs(request.videos, database)
        request_snapshot = request.model_dump(mode="json")
        shortform_context = _find_shortform_context(db, request)
        if shortform_context:
            request_snapshot["_shortform_context"] = shortform_context
        run = EditingRun(
            id=f"edit_{uuid.uuid4().hex}",
            status=EditingRunStatus.QUEUED.value,
            stage=EditingRunStage.QUEUED.value,
            progress=0,
            stage_started_at=datetime.now(timezone.utc),
            request_snapshot=request_snapshot,
            revision_action=request.revision,
            warnings=[],
            video_context=[],
            missing_scene_roles=[],
            available_options=[],
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def create_revision(
        self,
        db: Session,
        parent_run_id: str,
        request: EditingRevisionRequest,
    ) -> EditingRun:
        self._validate_video_limit(request.videos)
        parent = db.get(EditingRun, parent_run_id)
        if parent is None:
            raise EditingDomainError("EDITING_RUN_NOT_FOUND", "Editing run not found.", status_code=404)
        if parent.status not in {
            EditingRunStatus.COMPLETED.value,
            EditingRunStatus.SOURCE_GAP.value,
        }:
            raise EditingDomainError(
                "EDITING_RUN_NOT_REVISION_READY",
                "A revision can be created only from a completed or source-gap run.",
                status_code=409,
            )
        parent_snapshot = dict(parent.request_snapshot or {})
        shortform_context = parent_snapshot.pop("_shortform_context", None)
        snapshot = EditingRunCreateRequest.model_validate(parent_snapshot)
        database = self._get_active_database(db, snapshot.selected_shortform)
        normalized_revision_videos = _normalize_video_inputs(request.videos, database)
        parent_identity = {
            video.video_id: (video.shooting_scene_order, video.shooting_element_id)
            for video in snapshot.videos
        }
        refreshed_identity = {
            video.video_id: (video.shooting_scene_order, video.shooting_element_id)
            for video in normalized_revision_videos
        }
        existing_changed = any(
            refreshed_identity.get(video_id) != order
            for video_id, order in parent_identity.items()
        )
        completed_video_set_changed = (
            parent.status == EditingRunStatus.COMPLETED.value
            and set(refreshed_identity) != set(parent_identity)
        )
        if existing_changed or completed_video_set_changed:
            raise EditingDomainError(
                "EDITING_REVISION_VIDEO_MISMATCH",
                "Revision videos must preserve existing video IDs and shooting order. "
                "New videos are accepted only for a source-gap revision.",
                status_code=409,
            )
        snapshot.videos = normalized_revision_videos
        snapshot.revision = request.revision_action
        revision_snapshot = snapshot.model_dump(mode="json")
        if shortform_context:
            revision_snapshot["_shortform_context"] = shortform_context
        run = EditingRun(
            id=f"edit_{uuid.uuid4().hex}",
            parent_run_id=parent.id,
            status=EditingRunStatus.QUEUED.value,
            stage=EditingRunStage.QUEUED.value,
            progress=0,
            stage_started_at=datetime.now(timezone.utc),
            request_snapshot=revision_snapshot,
            revision_action=request.revision_action,
            warnings=[],
            video_context=[],
            missing_scene_roles=[],
            available_options=[],
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def _validate_video_limit(self, videos: list[Any]) -> None:
        limit = self.settings.editing_max_videos_per_run
        if len(videos) > limit:
            raise EditingDomainError(
                "EDITING_VIDEO_LIMIT_EXCEEDED",
                f"At most {limit} videos can be processed in one editing run.",
                status_code=422,
            )

    def execute(self, db: Session, run_id: str) -> EditingRun:
        run = db.get(EditingRun, run_id)
        if run is None:
            raise EditingDomainError("EDITING_RUN_NOT_FOUND", "Editing run not found.", status_code=404)
        if run.status != EditingRunStatus.QUEUED.value:
            return run

        reset_usage()
        try:
            raw_snapshot = dict(run.request_snapshot or {})
            shortform_context = raw_snapshot.pop("_shortform_context", {})
            request = EditingRunCreateRequest.model_validate(raw_snapshot)
            database_record = self._get_active_database(db, request.selected_shortform)
            database_payload = _database_payload(database_record)
            project_payload = request.project.model_dump(mode="json")
            if shortform_context:
                project_payload["shortform_context"] = shortform_context
            parent = db.get(EditingRun, run.parent_run_id) if run.parent_run_id else None
            parent_recipe = parent.recipe if parent is not None else None

            run.status = EditingRunStatus.RUNNING.value
            run.started_at = datetime.now(timezone.utc)
            self._set_stage(db, run, EditingRunStage.PREPARING_VIDEO_CONTEXT, 10)
            contexts = self.video_context_builder.build(request.videos)
            run.video_context = [persistable_video_context(context) for context in contexts]
            restore_checkpoint = getattr(self.llm, "restore_analysis_checkpoint", None)
            if callable(restore_checkpoint):
                restore_checkpoint(run.analysis_checkpoint)

            def save_analysis_checkpoint(checkpoint: dict[str, Any]) -> None:
                run.analysis_checkpoint = checkpoint
                self._sync_usage(run)
                db.commit()

            def update_graph_stage(stage: str, progress: int) -> None:
                self._set_stage(
                    db,
                    run,
                    EditingRunStage(stage),
                    max(run.progress, progress),
                )

            planning_error: EditingLLMError | None = None
            try:
                result = self.graph.invoke(
                    {
                        "domain_context": self.domain_context,
                        "project": project_payload,
                        "selected_shortform": request.selected_shortform.model_dump(mode="json"),
                        "video_editing_db": database_payload,
                        "videos": [video.model_dump(mode="json") for video in request.videos],
                        "video_contexts": [
                            context.model_dump(mode="json") for context in contexts
                        ],
                        "parent_recipe": parent_recipe,
                        "revision_action": run.revision_action,
                        "max_repair_attempts": self.settings.editing_max_repair_attempts,
                        "repair_attempts": 0,
                        "stage_callback": update_graph_stage,
                        "checkpoint_callback": save_analysis_checkpoint,
                    }
                )
            except EditingLLMError as exc:
                if not _is_editing_plan_contract_error(exc):
                    raise
                planning_error = exc
                result = None

            if planning_error is not None:
                run.warnings = [
                    *(run.warnings or []),
                    (
                        "EDITING_PLAN_FALLBACK: 편집 GPT 결과 형식 검증에 실패하여 "
                        "촬영 순서 기반 기본 편집을 적용했습니다."
                    ),
                ]
                decision = self._build_ordered_fallback(
                    request, database_payload, contexts, shortform_context
                )
            else:
                assert result is not None
                if result.get("exhausted"):
                    errors = [
                        _format_validation_issue(item)
                        for item in result.get("validation_errors", [])
                    ]
                    raise EditingDomainError(
                        "EDITING_RECIPE_INVALID",
                        "Recipe validation failed after repair: " + "; ".join(errors),
                        status_code=500,
                    )
                decision = EditingPlanDecision.model_validate(result["decision"])

            if decision.outcome == "SOURCE_GAP":
                # A visual role mismatch must not strand the client waiting for a
                # render that will never exist. First ask the planner to use the
                # supported reduced structure; if it still refuses or produces an
                # invalid plan, fall back to a deterministic shooting-order edit.
                run.missing_scene_roles = decision.missing_scene_roles
                run.warnings = [
                    *(run.warnings or []),
                    "SOURCE_ROLE_MATCH_FALLBACK: 장면 매칭이 부족하여 촬영 순서 기반 편집을 적용했습니다.",
                ]
                try:
                    reduced = self.graph.invoke(
                        {
                            "domain_context": self.domain_context,
                            "project": project_payload,
                            "selected_shortform": request.selected_shortform.model_dump(mode="json"),
                            "video_editing_db": database_payload,
                            "videos": [video.model_dump(mode="json") for video in request.videos],
                            "video_contexts": [context.model_dump(mode="json") for context in contexts],
                            "parent_recipe": parent_recipe,
                            "revision_action": "USE_REDUCED_STRUCTURE",
                            "max_repair_attempts": self.settings.editing_max_repair_attempts,
                            "repair_attempts": 0,
                            "stage_callback": update_graph_stage,
                            "checkpoint_callback": save_analysis_checkpoint,
                        }
                    )
                    if reduced.get("exhausted"):
                        decision = self._build_ordered_fallback(
                            request, database_payload, contexts, shortform_context
                        )
                    else:
                        decision = EditingPlanDecision.model_validate(reduced["decision"])
                        if decision.outcome == "SOURCE_GAP":
                            decision = self._build_ordered_fallback(
                                request, database_payload, contexts, shortform_context
                            )
                except (EditingLLMError, ValidationError, ValueError, TypeError):
                    decision = self._build_ordered_fallback(
                        request, database_payload, contexts, shortform_context
                    )

            recipe = EditRecipe.model_validate(decision.recipe)
            publishing = PublishingResult.model_validate(decision.publishing)
            self._set_stage(db, run, EditingRunStage.RENDERING, 80)
            render_result = self.renderer.render(
                run_id=run.id,
                recipe=recipe,
                videos=request.videos,
                video_contexts=contexts,
                video_editing_db=database_payload,
            )
            run.recipe = recipe.model_dump(mode="json")
            run.publishing_result = publishing.model_dump(mode="json")
            run.render_result = render_result.model_dump(mode="json")
            run.status = EditingRunStatus.COMPLETED.value
            run.stage = EditingRunStage.COMPLETED.value
            run.progress = 100
            run.finished_at = datetime.now(timezone.utc)
            self._sync_usage(run)
            db.commit()
            db.refresh(run)
            return run
        except Exception as exc:
            db.rollback()
            failed = db.get(EditingRun, run_id)
            if failed is not None:
                failed.status = EditingRunStatus.FAILED.value
                failed.stage = EditingRunStage.FAILED.value
                failed.progress = min(failed.progress, 99)
                failed.error_message = _safe_error_message(exc)
                failed.finished_at = datetime.now(timezone.utc)
                self._sync_usage(failed)
                db.commit()
            raise

    def _build_ordered_fallback(
        self,
        request: EditingRunCreateRequest,
        video_editing_db: dict[str, Any],
        contexts: list[Any],
        shortform_context: dict[str, Any] | None = None,
    ) -> EditingPlanDecision:
        """Build a conservative renderable recipe without scene-role inference."""
        rules = video_editing_db.get("editing_rules") or {}
        min_cut_ms = max(300, int(rules.get("min_cut_duration_ms") or 0))
        max_duration_ms = min(
            int(float(rules.get("max_duration_sec") or 90) * 1000),
            self.settings.editing_max_output_duration_seconds * 1000,
        )
        usable = [
            context
            for context in sorted(contexts, key=lambda item: item.shooting_scene_order)
            if context.duration_ms >= min_cut_ms
        ]
        max_clip_count = max(1, max_duration_ms // min_cut_ms)
        usable = usable[:max_clip_count]
        if not usable:
            raise EditingDomainError(
                "EDITING_SOURCE_TOO_SHORT",
                "No uploaded video is long enough to produce a valid fallback edit.",
                status_code=422,
            )

        target_per_clip_ms = max(
            min_cut_ms,
            min(3_000, max_duration_ms // len(usable)),
        )
        timeline: list[RecipeClip] = []
        cursor = 0
        for index, context in enumerate(usable, start=1):
            remaining = max_duration_ms - cursor
            duration = min(context.duration_ms, target_per_clip_ms, remaining)
            if duration < min_cut_ms:
                break
            timeline.append(
                RecipeClip(
                    clip_order=index,
                    video_id=context.video_id,
                    source_start_ms=0,
                    source_end_ms=duration,
                    timeline_start_ms=cursor,
                    speed=1.0,
                    crop_mode="SUBJECT_CENTER",
                    transition_in=None,
                    transition_out="CUT",
                    caption=None,
                    effects=[],
                )
            )
            cursor += duration

        subject = request.project.promotion_subject
        subject_name = _promotion_subject_name(subject)
        fallback_copy = _fallback_copy_context(shortform_context or {})
        _apply_fallback_promotional_captions(timeline, subject_name, fallback_copy)
        search_keyword = _fallback_search_keyword(
            str(
                video_editing_db.get("recommendation_title")
                or video_editing_db.get("name")
                or request.selected_shortform.editing_template_id
            )
        )
        recipe = EditRecipe(
            recipe_version=1,
            editing_template_id=request.selected_shortform.editing_template_id,
            editing_template_version=request.selected_shortform.editing_template_version,
            source_type="VIDEO_ONLY",
            timeline=timeline,
            cta=RecipeCta(
                text=_fit_caption(
                    fallback_copy.get("cta") or f"{subject_name}, 지금 만나보세요"
                )
            ),
        )
        recipe = self.effect_planner.apply_recipe(
            recipe,
            produced_frame_context={"observations": []},
            video_editing_db=video_editing_db,
        )
        validation_errors = self.validator.validate(
            recipe,
            selected_shortform=request.selected_shortform,
            video_editing_db=video_editing_db,
            video_contexts=contexts,
            project=request.project.model_dump(mode="json"),
        )
        if validation_errors:
            errors = "; ".join(_format_validation_issue(item) for item in validation_errors)
            raise EditingDomainError(
                "EDITING_FALLBACK_INVALID",
                "Fallback recipe validation failed: " + errors,
                status_code=500,
            )
        return EditingPlanDecision(
            outcome="RECIPE",
            recipe=recipe,
            publishing=PublishingResult(
                title=_fit_caption(fallback_copy.get("title") or f"{subject_name} 공개"),
                caption=_fit_caption(
                    fallback_copy.get("body")
                    or f"{subject_name}의 매력을 짧은 영상으로 확인해 보세요."
                ),
                hashtags=_fallback_hashtags(subject_name),
                track=PublishingTrack(
                    mode="SUGGESTED",
                    search_keyword=search_keyword,
                ),
                post_note=(
                    f"플랫폼 음원 검색에서 ‘{search_keyword}’을 검색해 직접 추가해주세요."
                ),
            ),
            missing_scene_roles=[],
            available_options=[],
            rationale="장면 역할 매칭 실패 후 촬영 순서 기반 자동 축소 편집",
        )

    @staticmethod
    def mark_enqueue_failed(db: Session, run: EditingRun) -> None:
        run.status = EditingRunStatus.FAILED.value
        run.stage = EditingRunStage.FAILED.value
        run.error_message = "TASK_ENQUEUE_FAILED: Could not publish the editing task."
        run.finished_at = datetime.now(timezone.utc)
        db.commit()

    def result(self, run: EditingRun) -> EditingRunResultResponse:
        warnings = [str(item) for item in (run.warnings or [])]
        recipe = _recipe_for_result(run.recipe)
        if run.recipe and recipe is None:
            warnings.append(
                "LEGACY_RECIPE_UNAVAILABLE: 이전 편집 레시피는 현재 형식으로 변환할 수 없습니다."
            )
        try:
            publishing = _publishing_for_result(run)
        except ValidationError:
            publishing = None
            warnings.append(
                "LEGACY_PUBLISHING_UNAVAILABLE: 이전 게시 문구는 현재 형식으로 변환할 수 없습니다."
            )
        return EditingRunResultResponse(
            run_id=run.id,
            status=EditingRunStatus(run.status),
            recipe=recipe,
            render=run.render_result,
            publishing=publishing,
            warnings=warnings,
            missing_scene_roles=[str(item) for item in (run.missing_scene_roles or [])],
            available_options=run.available_options or [],
        )

    def _set_stage(
        self,
        db: Session,
        run: EditingRun,
        stage: EditingRunStage,
        progress: int,
    ) -> None:
        if run.stage != stage.value:
            run.stage_started_at = datetime.now(timezone.utc)
        run.stage = stage.value
        run.progress = progress
        self._sync_usage(run)
        db.commit()

    def _sync_usage(self, run: EditingRun) -> None:
        usage = usage_snapshot()
        run.llm_request_count = usage.request_count
        run.llm_input_tokens = usage.input_tokens
        run.llm_output_tokens = usage.output_tokens
        run.llm_estimated_cost_usd = round(
            usage.input_tokens * self.settings.editing_input_cost_per_million_usd / 1_000_000
            + usage.output_tokens
            * self.settings.editing_output_cost_per_million_usd
            / 1_000_000,
            8,
        )

    @staticmethod
    def _get_active_database(
        db: Session,
        selected: SelectedShortform,
    ) -> VideoEditingDBRecord:
        database_record = db.get(
            VideoEditingDBRecord,
            (selected.editing_template_id, selected.editing_template_version),
        )
        if database_record is None:
            raise EditingDomainError(
                "VIDEO_EDITING_DB_NOT_FOUND",
                "The selected video-editing DB version was not found.",
                status_code=404,
            )
        if database_record.status not in {"ACTIVE", "ARCHIVED"}:
            raise EditingDomainError(
                "VIDEO_EDITING_DB_INACTIVE",
                "The selected video-editing DB version is not executable.",
                status_code=409,
            )
        return database_record


def validate_editing_runtime() -> dict[str, bool]:
    settings = get_settings()
    return {
        "openai": bool(settings.openai_api_key.strip() and settings.editing_openai_model.strip()),
        "renderer": _renderer_service_ready(),
        "reals_registry": _reals_registry_ready(),
        "ffprobe": _command_exists(settings.editing_ffprobe_path),
        "ffmpeg": _command_exists(settings.editing_ffmpeg_path),
    }


def _renderer_service_ready() -> bool:
    settings = get_settings()
    url = settings.editing_renderer_url.rstrip("/")
    if not url:
        return False
    try:
        response = httpx.get(
            f"{url}/health/ready",
            timeout=settings.editing_renderer_health_timeout_seconds,
        )
        if response.status_code != 200:
            return False
        payload = response.json()
        return isinstance(payload, dict) and payload.get("ready") is True
    except (httpx.HTTPError, ValueError, TypeError):
        return False


def _command_exists(command: str) -> bool:
    path = Path(command)
    return path.is_file() if path.parent != Path(".") else shutil.which(command) is not None


def _reals_registry_ready() -> bool:
    try:
        get_reals_registry()
    except RealsRegistryError:
        return False
    return True


def _database_payload(database_record: VideoEditingDBRecord) -> dict[str, Any]:
    return {
        "editing_template_id": database_record.template_id,
        "editing_template_version": database_record.version,
        "name": database_record.name,
        "recommendation_title": database_record.recommendation_title,
        "recommendation_concept": database_record.recommendation_concept,
        "recommendation_metadata": database_record.recommendation_metadata or {},
        "shooting_guide": database_record.shooting_guide or {},
        "editing_rules": database_record.editing_rules or {},
        # Existing DB column only: Gemini reference-video evidence is preserved
        # here, so the Editing Agent can match user frames to the original-video
        # segment/effect context without extending the video-editing DB schema.
        "reference_evidence": database_record.evidence_summary or {},
    }


def _normalize_video_inputs(
    videos: list[Any],
    database_record: VideoEditingDBRecord,
) -> list[Any]:
    metadata = database_record.recommendation_metadata or {}
    is_information = str(metadata.get("format_type") or "") == "정보형"
    if not is_information:
        if any(video.shooting_scene_order is None for video in videos):
            raise EditingDomainError(
                "SHOOTING_SCENE_ORDER_REQUIRED",
                "밈과 챌린지 영상은 shooting_scene_order가 필요합니다.",
                status_code=422,
            )
        return sorted(videos, key=lambda video: int(video.shooting_scene_order))

    elements = (database_record.shooting_guide or {}).get("shooting_elements") or []
    element_orders = {
        str(item.get("element_id")): int(item.get("display_order") or 0)
        for item in elements
        if item.get("element_id")
    }
    if not element_orders:
        raise EditingDomainError(
            "INFORMATIONAL_SHOOTING_ELEMENTS_MISSING",
            "정보형 템플릿에 활성 촬영 요소가 없습니다.",
            status_code=409,
        )
    missing = [video.video_id for video in videos if not video.shooting_element_id]
    if missing:
        raise EditingDomainError(
            "SHOOTING_ELEMENT_ID_REQUIRED",
            "정보형 영상은 각 업로드에 shooting_element_id가 필요합니다.",
            status_code=422,
        )
    unknown = sorted(
        {
            str(video.shooting_element_id)
            for video in videos
            if str(video.shooting_element_id) not in element_orders
        }
    )
    if unknown:
        raise EditingDomainError(
            "SHOOTING_ELEMENT_ID_INVALID",
            "선택한 템플릿에 없는 촬영 요소입니다: " + ", ".join(unknown),
            status_code=422,
        )
    ordered = sorted(
        enumerate(videos),
        key=lambda pair: (
            element_orders[str(pair[1].shooting_element_id)],
            pair[0],
        ),
    )
    return [
        video.model_copy(update={"shooting_scene_order": index})
        for index, (_, video) in enumerate(ordered, start=1)
    ]


def _find_shortform_context(
    db: Session,
    request: EditingRunCreateRequest,
) -> dict[str, Any]:
    """Resolve and freeze the AI-owned brief selected by recommendation_id."""
    recommendation_id = request.selected_shortform.recommendation_id
    sessions = db.scalars(
        select(ShortformSession)
        .where(ShortformSession.store_id == request.project.store_id)
        .order_by(ShortformSession.updated_at.desc())
        .limit(50)
    ).all()
    matched: tuple[ShortformSession, dict[str, Any]] | None = None
    for item in sessions:
        stored = dict(item.current_recommendation or {})
        batch = stored.get("recommendations")
        recommendations = batch if isinstance(batch, list) else [stored]
        selected = next(
            (
                value
                for value in recommendations
                if isinstance(value, dict)
                and str(value.get("recommendation_id") or "") == recommendation_id
            ),
            None,
        )
        if selected is not None:
            matched = (item, dict(selected))
            break
    if matched is None:
        return {}
    session, selected_recommendation = matched

    state = dict(session.project_state or {})
    store_context = dict(session.store_context or {})
    store = dict(store_context.get("store") or {})
    store.pop("store_photos", None)
    safe_store_context = {
        "store": store,
        "representative_menus": list(store_context.get("representative_menus") or []),
        "trade_area": store_context.get("trade_area"),
    }
    user_statements = [
        str(item.get("content") or "").strip()[:500]
        for item in list(session.conversation or [])[-40:]
        if isinstance(item, dict)
        and item.get("role") == "user"
        and str(item.get("content") or "").strip()
    ][-12:]
    return {
        "session_id": session.id,
        "recommendation_id": recommendation_id,
        "project_state": {
            key: state.get(key)
            for key in (
                "promotion_category",
                "promotion_subject",
                "promotion_objective",
                "face_exposure",
                "creative_preferences",
                "secondary_information",
                "facts_from_user",
                "brief_confirmed",
            )
        },
        "store_context": safe_store_context,
        "recommendation": selected_recommendation,
        "recent_user_statements": user_statements,
    }


def _is_editing_plan_contract_error(exc: EditingLLMError) -> bool:
    message = str(exc)
    if not (
        "schema=editing_plan;" in message
        or "schema=editing_plan_repair;" in message
    ):
        return False
    return any(
        f"reason={reason}" in message
        for reason in (
            "schema_validation",
            "invalid_json",
            "invalid_structured_output",
            "empty_output",
            "incomplete_",
            "response_status_",
            "refusal",
        )
    )


def _promotion_subject_name(subject: dict[str, Any]) -> str:
    for key in ("name", "menu_name", "description", "title"):
        value = " ".join(str(subject.get(key) or "").split())
        if value:
            return value[:40]
    for item in subject.get("elements") or []:
        value = " ".join(str(item or "").split())
        if value:
            return value[:40]
    return "오늘의 추천"


def _apply_fallback_promotional_captions(
    timeline: list[RecipeClip],
    subject_name: str,
    copy_context: dict[str, str] | None = None,
) -> None:
    """Guarantee useful, evidence-safe copy when the LLM fallback is used."""
    if not timeline:
        return
    caption_total = min(3, len(timeline))
    if len(timeline) <= caption_total:
        indices = list(range(len(timeline)))
    else:
        indices = [0, (len(timeline) - 1) // 2, len(timeline) - 2]
    context = copy_context or {}
    texts = [
        _fit_caption(context.get("hook") or f"{subject_name}, 지금 공개합니다"),
        _fit_caption(context.get("detail") or "하나씩 공개되는 특별한 순간"),
        _fit_caption(context.get("support") or "눈으로 먼저 만나는 매력"),
    ]
    styles = ["HOOK", "CAPTION_EMPHASIS", "CAPTION"]
    positions = ["TOP", "MIDDLE", "TOP"]
    for order, index in enumerate(indices[:caption_total]):
        clip = timeline[index]
        output_duration = int(round((clip.source_end_ms - clip.source_start_ms) / clip.speed))
        typewriter_units = len("".join(texts[order].split()))
        typewriter_required_ms = max(0, typewriter_units - 1) * 80 + 600
        if order == 0 and typewriter_units <= 18 and output_duration >= typewriter_required_ms:
            motion_id = "TYPEWRITER"
        elif order < 2:
            motion_id = "POP"
        else:
            motion_id = "NONE"
        clip.caption = RecipeCaption(
            text=texts[order],
            start_ms=clip.timeline_start_ms,
            end_ms=clip.timeline_start_ms + output_duration,
            position=positions[order],
            style_id=styles[order],
            motion_id=motion_id,
            font_weight="BOLD" if order < 2 else "SEMIBOLD",
            scale=1.0,
        )


def _fallback_copy_context(shortform_context: dict[str, Any]) -> dict[str, str]:
    state = dict(shortform_context.get("project_state") or {})
    subject = dict(state.get("promotion_subject") or {})
    subject_name = str(subject.get("name") or "").strip()
    facts = [
        str(value).strip()
        for value in dict(state.get("facts_from_user") or {}).values()
        if str(value).strip()
    ]
    details = [
        str(value).strip()
        for value in list(state.get("secondary_information") or [])
        if str(value).strip()
    ]
    preferences = [
        str(value).strip()
        for value in list(state.get("creative_preferences") or [])
        if str(value).strip()
    ]
    specific = list(dict.fromkeys([*facts, *details]))
    if not subject_name and not specific:
        return {}
    hook = subject_name or specific[0]
    detail = specific[0] if specific and specific[0] != hook else ""
    support = specific[1] if len(specific) > 1 else (preferences[0] if preferences else "")
    objective = str(state.get("promotion_objective") or "").lower()
    cta_suffix = {
        "visit": "직접 만나보세요",
        "sales": "지금 만나보세요",
        "reservation_inquiry": "지금 문의해보세요",
        "new_customer": "새롭게 만나보세요",
        "revisit": "다시 만나보세요",
    }.get(objective, "더 알아보세요")
    body_parts = list(dict.fromkeys([item for item in [subject_name, *specific[:2]] if item]))
    return {
        "hook": hook,
        "detail": detail,
        "support": support,
        "cta": f"{subject_name or hook}, {cta_suffix}",
        "title": f"{subject_name or hook}의 포인트",
        "body": " · ".join(body_parts),
    }


def _fit_caption(value: str, limit: int = 40) -> str:
    text = " ".join(value.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _fallback_search_keyword(value: str) -> str:
    keyword = value.strip()
    lowered = keyword.lower()
    known_keywords = {
        "jujutsu": "주술회전",
        "otsukare": "오츠카레 썸머",
        "cafe_recommendation": "카페 추천 릴스",
    }
    for marker, known in known_keywords.items():
        if marker in lowered:
            return known
    for suffix in ("트랜지션", "챌린지", "릴스", "숏폼", "포맷"):
        keyword = keyword.replace(suffix, " ")
    keyword = " ".join(keyword.split())
    return (keyword or "트렌드 음원")[:80]


def _fallback_hashtags(subject_name: str) -> list[str]:
    subject_tag = "#" + "".join(subject_name.split())
    candidates = [
        subject_tag,
        "#매장소개",
        "#가게소개",
        "#동네맛집",
        "#숏폼",
        "#릴스",
    ]
    return list(dict.fromkeys(candidates))[:5]


def _publishing_for_result(run: EditingRun) -> PublishingResult | None:
    if not run.publishing_result:
        return None
    data = dict(run.publishing_result)
    raw_caption = str(data.get("caption") or "").strip()
    project = (run.request_snapshot or {}).get("project") or {}
    subject = project.get("promotion_subject") or {}
    subject_name = str(subject.get("name") or "오늘의 추천")[:40]
    data["title"] = _strip_legacy_operational_copy(str(data.get("title") or ""))
    data["caption"] = _strip_legacy_operational_copy(raw_caption)
    if not data.get("title"):
        data["title"] = f"{subject_name}의 매력을 만나보세요"
    if not data.get("caption"):
        data["caption"] = (
            f"{subject_name}의 모습을 짧은 영상으로 확인해 보세요."
        )

    hashtags = [str(value) for value in (data.get("hashtags") or [])]
    for fallback in ("#숏폼", "#릴스", "#매장소개", "#가게소개", "#동네맛집"):
        if len(hashtags) >= 5:
            break
        if fallback not in hashtags:
            hashtags.append(fallback)
    data["hashtags"] = hashtags

    selected = (run.request_snapshot or {}).get("selected_shortform") or {}
    fallback_keyword = _fallback_search_keyword(
        str(selected.get("editing_template_id") or "")
    )
    track = dict(data.get("track") or {})
    track["start_sec"] = None
    track["end_sec"] = None
    if track.get("title"):
        track["mode"] = "FIXED"
    else:
        keyword = str(track.get("search_keyword") or fallback_keyword)
        track.update(
            {
                "mode": "SUGGESTED",
                "title": None,
                "artist": None,
                "mood": track.get("mood"),
                "search_keyword": keyword,
            }
        )
        data["post_note"] = (
            f"플랫폼 음원 검색에서 ‘{keyword}’을 검색해 직접 추가해주세요."
        )
    data["track"] = track
    return PublishingResult.model_validate(data)


def _recipe_for_result(raw: dict[str, Any] | None) -> EditRecipe | None:
    """Adapt persisted pre-contract recipes without leaking validation errors as HTTP 500."""

    if not raw:
        return None
    data = dict(raw)
    cta = dict(data.get("cta") or {})
    cleaned = _strip_legacy_operational_copy(str(cta.get("text") or ""))
    if not cleaned:
        cleaned = "영상의 포인트를 지금 확인해보세요"
    cta["text"] = cleaned[:80]
    data["cta"] = cta
    try:
        return EditRecipe.model_validate(data)
    except ValidationError:
        return None


def _strip_legacy_operational_copy(value: str) -> str:
    text = value.strip()
    positions = [
        text.find(marker)
        for marker in ("음악은", "음원은", "플랫폼에서", "업로드 후", "게시 시")
        if marker in text
    ]
    if positions:
        text = text[: min(positions)].rstrip(" ,.!·")
    return text


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, EditingDomainError):
        return str(exc)[:1000]
    return f"{type(exc).__name__}: {str(exc)}"[:1000]


def _format_validation_issue(value: Any) -> str:
    if not isinstance(value, dict):
        return str(value)
    code = str(value.get("code") or "VALIDATION_ERROR")
    path = str(value.get("path") or "recipe")
    message = str(value.get("message") or "Recipe validation failed.")
    return f"{code} at {path}: {message}"


def _load_domain_context() -> str:
    return (Path(__file__).with_name("context.md")).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_editing_agent_service() -> EditingAgentService:
    return EditingAgentService()


def create_response(run: EditingRun) -> EditingRunCreateResponse:
    return EditingRunCreateResponse(
        run_id=run.id,
        parent_run_id=run.parent_run_id,
        status=EditingRunStatus(run.status),
        task_id=run.celery_task_id,
    )


def revision_response(run: EditingRun) -> EditingRevisionResponse:
    if run.parent_run_id is None:
        raise ValueError("Revision run has no parent_run_id")
    return EditingRevisionResponse(
        run_id=run.id,
        parent_run_id=run.parent_run_id,
        status=EditingRunStatus(run.status),
        task_id=run.celery_task_id,
    )
