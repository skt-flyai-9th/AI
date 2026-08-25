from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.agents.editing.graph import build_editing_graph
from app.agents.editing.llm import EditingLLM, OpenAIEditingLLM
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
    ) -> None:
        self.llm = llm or OpenAIEditingLLM()
        self.video_context_builder = video_context_builder or FFmpegVideoContextBuilder()
        self.validator = validator or EditRecipeValidator()
        self.renderer = renderer or HttpEditingRenderer()
        self.settings = get_settings()
        self.domain_context = _load_domain_context()
        self.graph = build_editing_graph(self.llm, self.validator)

    def create_run(self, db: Session, request: EditingRunCreateRequest) -> EditingRun:
        self._validate_video_limit(request.videos)
        self._get_active_database(db, request.selected_shortform)
        run = EditingRun(
            id=f"edit_{uuid.uuid4().hex}",
            status=EditingRunStatus.QUEUED.value,
            stage=EditingRunStage.QUEUED.value,
            progress=0,
            request_snapshot=request.model_dump(mode="json"),
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
        snapshot = EditingRunCreateRequest.model_validate(parent.request_snapshot)
        parent_identity = {
            video.video_id: video.shooting_scene_order for video in snapshot.videos
        }
        refreshed_identity = {
            video.video_id: video.shooting_scene_order for video in request.videos
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
        snapshot.videos = sorted(request.videos, key=lambda video: video.shooting_scene_order)
        snapshot.revision = request.revision_action
        self._get_active_database(db, snapshot.selected_shortform)
        run = EditingRun(
            id=f"edit_{uuid.uuid4().hex}",
            parent_run_id=parent.id,
            status=EditingRunStatus.QUEUED.value,
            stage=EditingRunStage.QUEUED.value,
            progress=0,
            request_snapshot=snapshot.model_dump(mode="json"),
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

        try:
            request = EditingRunCreateRequest.model_validate(run.request_snapshot)
            database_record = self._get_active_database(db, request.selected_shortform)
            database_payload = _database_payload(database_record)
            parent = db.get(EditingRun, run.parent_run_id) if run.parent_run_id else None
            parent_recipe = parent.recipe if parent is not None else None

            run.status = EditingRunStatus.RUNNING.value
            run.started_at = datetime.now(timezone.utc)
            self._set_stage(db, run, EditingRunStage.PREPARING_VIDEO_CONTEXT, 10)
            contexts = self.video_context_builder.build(request.videos)
            run.video_context = [persistable_video_context(context) for context in contexts]

            def update_graph_stage(stage: str, progress: int) -> None:
                self._set_stage(
                    db,
                    run,
                    EditingRunStage(stage),
                    max(run.progress, progress),
                )

            result = self.graph.invoke(
                {
                    "domain_context": self.domain_context,
                    "project": request.project.model_dump(mode="json"),
                    "selected_shortform": request.selected_shortform.model_dump(mode="json"),
                    "video_editing_db": database_payload,
                    "videos": [video.model_dump(mode="json") for video in request.videos],
                    "video_contexts": [context.model_dump(mode="json") for context in contexts],
                    "parent_recipe": parent_recipe,
                    "revision_action": run.revision_action,
                    "max_repair_attempts": self.settings.editing_max_repair_attempts,
                    "repair_attempts": 0,
                    "stage_callback": update_graph_stage,
                }
            )
            if result.get("exhausted"):
                errors = [_format_validation_issue(item) for item in result.get("validation_errors", [])]
                raise EditingDomainError(
                    "EDITING_RECIPE_INVALID",
                    "Recipe validation failed after repair: " + "; ".join(errors),
                    status_code=500,
                )

            decision = EditingPlanDecision.model_validate(result["decision"])
            if decision.outcome == "SOURCE_GAP":
                run.status = EditingRunStatus.SOURCE_GAP.value
                run.stage = EditingRunStage.COMPLETED.value
                run.progress = 100
                run.missing_scene_roles = decision.missing_scene_roles
                run.available_options = decision.available_options
                run.finished_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(run)
                return run

            recipe = EditRecipe.model_validate(decision.recipe)
            publishing = PublishingResult.model_validate(decision.publishing).model_copy(
                update={"post_note": "음원은 게시 시 플랫폼 내에서 추가해주세요."}
            )
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
                db.commit()
            raise

    @staticmethod
    def mark_enqueue_failed(db: Session, run: EditingRun) -> None:
        run.status = EditingRunStatus.FAILED.value
        run.stage = EditingRunStage.FAILED.value
        run.error_message = "TASK_ENQUEUE_FAILED: Could not publish the editing task."
        run.finished_at = datetime.now(timezone.utc)
        db.commit()

    def result(self, run: EditingRun) -> EditingRunResultResponse:
        return EditingRunResultResponse(
            run_id=run.id,
            status=EditingRunStatus(run.status),
            recipe=EditRecipe.model_validate(run.recipe) if run.recipe else None,
            render=run.render_result,
            publishing=run.publishing_result,
            warnings=[str(item) for item in (run.warnings or [])],
            missing_scene_roles=[str(item) for item in (run.missing_scene_roles or [])],
            available_options=run.available_options or [],
        )

    @staticmethod
    def _set_stage(
        db: Session,
        run: EditingRun,
        stage: EditingRunStage,
        progress: int,
    ) -> None:
        run.stage = stage.value
        run.progress = progress
        db.commit()

    @staticmethod
    def _get_active_database(
        db: Session,
        selected: SelectedShortform,
    ) -> VideoEditingDBRecord:
        database_record = db.get(
            VideoEditingDBRecord,
            (selected.video_editing_db_id, selected.video_editing_db_version),
        )
        if database_record is None:
            raise EditingDomainError(
                "VIDEO_EDITING_DB_NOT_FOUND",
                "The selected video-editing DB version was not found.",
                status_code=404,
            )
        if database_record.status != "ACTIVE":
            raise EditingDomainError(
                "VIDEO_EDITING_DB_INACTIVE",
                "The selected video-editing DB version is not ACTIVE.",
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
        "video_editing_db_id": database_record.template_id,
        "video_editing_db_version": database_record.version,
        "name": database_record.name,
        "recommendation_title": database_record.recommendation_title,
        "recommendation_concept": database_record.recommendation_concept,
        "shooting_guide": database_record.shooting_guide or {},
        "editing_rules": database_record.editing_rules or {},
        # Existing DB column only: Gemini reference-video evidence is preserved
        # here, so the Editing Agent can match user frames to the original-video
        # segment/effect context without extending the video-editing DB schema.
        "reference_evidence": database_record.evidence_summary or {},
    }


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
