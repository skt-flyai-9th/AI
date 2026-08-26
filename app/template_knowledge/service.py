from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.challenge import Challenge
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.models.template_update_candidate import TemplateUpdateCandidate
from app.models.template_video_analysis import TemplateVideoAnalysis
from app.models.template_knowledge_run import TemplateKnowledgeRun
from app.models.trade_area_analysis import TradeAreaAnalysis
from app.models.trade_area_db_record import TradeAreaDBRecord
from app.schemas.template_knowledge import (
    CandidateDecision,
    CandidateRejection,
    EditingCandidateCreate,
    VideoEditingDBContent,
    EditingVideoInsight,
    TemplateCandidateRead,
    TemplateCandidateStatus,
    TemplateType,
    TemplateKnowledgeOperation,
    TemplateKnowledgeRunResult,
    TemplateKnowledgeRunStatus,
    TemplateVersionRead,
    TemplateVersionStatus,
    TradeAreaAnalysisRead,
    TradeAreaAnalysisResult,
    TradeAreaAnalyzeRequest,
    TradeAreaCandidateCreate,
    TradeAreaDBContent,
)
from app.template_knowledge.llm import (
    GeminiYouTubeVideoAnalyzer,
    OpenAITemplateCandidateGenerator,
    ReferenceVideoAnalyzer,
    TemplateCandidateGenerator,
    TemplateKnowledgeLLMError,
)
from app.template_knowledge.validation import TemplateCandidateValidator


class TemplateKnowledgeDomainError(RuntimeError):
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


class TemplateKnowledgeService:
    def __init__(
        self,
        *,
        generator: TemplateCandidateGenerator | None = None,
        video_analyzer: ReferenceVideoAnalyzer | None = None,
        validator: TemplateCandidateValidator | None = None,
    ) -> None:
        self.generator = generator or OpenAITemplateCandidateGenerator()
        self.video_analyzer = video_analyzer or GeminiYouTubeVideoAnalyzer()
        self.validator = validator or TemplateCandidateValidator()
        self.settings = get_settings()

    def create_run(
        self,
        db: Session,
        operation: TemplateKnowledgeOperation,
        request_payload: dict[str, Any],
    ) -> TemplateKnowledgeRun:
        run = TemplateKnowledgeRun(
            id=f"tkr_{uuid4().hex[:24]}",
            operation=operation.value,
            status=TemplateKnowledgeRunStatus.QUEUED.value,
            stage="QUEUED",
            progress=0,
            request_payload=request_payload,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def execute_run(self, db: Session, run_id: str) -> TemplateKnowledgeRun:
        run = self.get_run(db, run_id)
        if run.status != TemplateKnowledgeRunStatus.QUEUED.value:
            raise TemplateKnowledgeDomainError(
                "DATABASE_RUN_NOT_QUEUED",
                "Only a QUEUED database knowledge run can be executed.",
                status_code=409,
            )
        run.status = TemplateKnowledgeRunStatus.RUNNING.value
        run.stage = "PREPARING_EVIDENCE"
        run.progress = 10
        run.started_at = _now()
        db.commit()
        try:
            operation = TemplateKnowledgeOperation(run.operation)
            if operation == TemplateKnowledgeOperation.TRADE_AREA_CANDIDATE:
                run.stage = "GENERATING_DATABASE_CANDIDATE"
                run.progress = 35
                db.commit()
                candidate = self.create_trade_area_candidate(
                    db, TradeAreaCandidateCreate.model_validate(run.request_payload)
                )
                result = {
                    "candidate": TemplateCandidateRead.model_validate(candidate).model_dump(
                        mode="json"
                    )
                }
            elif operation == TemplateKnowledgeOperation.VIDEO_EDITING_CANDIDATE:
                run.stage = "ANALYZING_REFERENCE_VIDEOS"
                run.progress = 25
                db.commit()
                candidate = self.create_editing_candidate(
                    db, EditingCandidateCreate.model_validate(run.request_payload)
                )
                result = {
                    "candidate": TemplateCandidateRead.model_validate(candidate).model_dump(
                        mode="json"
                    )
                }
            else:
                run.stage = "ANALYZING_TRADE_AREA"
                run.progress = 35
                db.commit()
                analysis = self.analyze_trade_area(
                    db, TradeAreaAnalyzeRequest.model_validate(run.request_payload)
                )
                result = {"analysis": analysis.model_dump(mode="json")}
        except TemplateKnowledgeDomainError as exc:
            db.rollback()
            run = self.get_run(db, run_id)
            return self._fail_run(
                db,
                run,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
        except Exception:
            db.rollback()
            run = self.get_run(db, run_id)
            return self._fail_run(
                db,
                run,
                code="DATABASE_RUN_EXECUTION_FAILED",
                message="Template knowledge run failed unexpectedly.",
                retryable=True,
            )
        run.status = TemplateKnowledgeRunStatus.COMPLETED.value
        run.stage = "COMPLETED"
        run.progress = 100
        run.result = result
        run.error = None
        run.error_message = None
        run.finished_at = _now()
        db.commit()
        db.refresh(run)
        return run

    def get_run(self, db: Session, run_id: str) -> TemplateKnowledgeRun:
        run = db.get(TemplateKnowledgeRun, run_id)
        if run is None:
            raise TemplateKnowledgeDomainError(
                "DATABASE_RUN_NOT_FOUND", "Database knowledge run was not found.", status_code=404
            )
        return run

    def run_result(self, db: Session, run_id: str) -> TemplateKnowledgeRunResult:
        run = self.get_run(db, run_id)
        if run.status != TemplateKnowledgeRunStatus.COMPLETED.value or run.result is None:
            raise TemplateKnowledgeDomainError(
                "DATABASE_RUN_NOT_COMPLETED",
                "Template knowledge result is not available yet.",
                status_code=409,
                retryable=run.status
                in {
                    TemplateKnowledgeRunStatus.QUEUED.value,
                    TemplateKnowledgeRunStatus.RUNNING.value,
                },
            )
        return TemplateKnowledgeRunResult(
            run_id=run.id,
            operation=TemplateKnowledgeOperation(run.operation),
            status=TemplateKnowledgeRunStatus(run.status),
            result=run.result,
        )

    def mark_enqueue_failed(
        self, db: Session, run: TemplateKnowledgeRun, message: str
    ) -> TemplateKnowledgeRun:
        return self._fail_run(
            db,
            run,
            code="DATABASE_RUN_ENQUEUE_FAILED",
            message=message,
            retryable=True,
        )

    @staticmethod
    def _fail_run(
        db: Session,
        run: TemplateKnowledgeRun,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> TemplateKnowledgeRun:
        run.status = TemplateKnowledgeRunStatus.FAILED.value
        run.stage = "FAILED"
        run.error = {"code": code, "message": message, "retryable": retryable}
        run.error_message = message
        run.finished_at = _now()
        db.commit()
        db.refresh(run)
        return run

    def create_trade_area_candidate(
        self,
        db: Session,
        request: TradeAreaCandidateCreate,
    ) -> TemplateUpdateCandidate:
        base = self._latest_trade_area(db, request.template_id)
        base_payload = _trade_area_payload(base) if base else None
        try:
            proposed = self.generator.generate_trade_area(
                template_id=request.template_id,
                base_payload=base_payload,
                evidence=request.evidence,
            )
        except TemplateKnowledgeLLMError as exc:
            raise _llm_domain_error(exc) from exc
        candidate = self.create_candidate_from_payload(
            db,
            template_type=TemplateType.TRADE_AREA,
            template_id=request.template_id,
            payload=proposed.model_dump(mode="json"),
            source_evidence={"trade_area_evidence": request.evidence.model_dump(mode="json")},
            generation_model=self.generator.model_name,
            requires_human_approval=(
                request.requires_human_approval or self.settings.database_require_human_approval
            ),
        )
        return candidate

    def create_editing_candidate(
        self,
        db: Session,
        request: EditingCandidateCreate,
    ) -> TemplateUpdateCandidate:
        trends = self._select_trends(db, request.trend_ids)
        if not trends:
            raise TemplateKnowledgeDomainError(
                "TRENDCLUSTER_REQUIRED",
                "No active trendcluster entry with a representative YouTube URL is available.",
                status_code=409,
            )
        trend_context = [_trend_payload(item) for item in trends]
        insights: list[EditingVideoInsight] = []
        analysis_ids: list[str] = []
        failures: list[dict[str, str]] = []
        for trend, context in zip(trends, trend_context, strict=True):
            url = str(context["representative_youtube_url"])
            try:
                analysis = self.analyze_reference_video(
                    db,
                    trend_id=trend.id,
                    youtube_url=url,
                    trend_context=context,
                    force=(request.force_video_analysis or request.rebuild_from_scratch),
                )
                insights.append(EditingVideoInsight.model_validate(analysis.insights))
                analysis_ids.append(analysis.id)
            except TemplateKnowledgeDomainError as exc:
                failures.append({"trend_id": trend.id, "code": exc.code, "message": str(exc)})
        if not insights:
            raise TemplateKnowledgeDomainError(
                "VIDEO_ANALYSIS_UNAVAILABLE",
                "Gemini could not produce editing evidence for any selected reference video.",
                status_code=502,
                retryable=True,
            )

        base = self._latest_editing(db, request.template_id)
        base_payload = (
            None
            if request.rebuild_from_scratch
            else (_editing_payload(base) if base else None)
        )
        try:
            proposed = self.generator.generate_editing(
                template_id=request.template_id,
                base_payload=base_payload,
                trend_context=trend_context,
                insights=insights,
            )
        except TemplateKnowledgeLLMError as exc:
            raise _llm_domain_error(exc) from exc
        payload = proposed.model_dump(mode="json")
        guide = payload["shooting_guide"]
        if len(guide["tasks"]) == len(guide["scenes"]):
            for index, task in enumerate(guide["tasks"]):
                task["display_order"] = index + 1
                task["scene_index"] = index
        payload["trend_ids"] = list(dict.fromkeys(item.trend_id for item in insights))
        return self.create_candidate_from_payload(
            db,
            template_type=TemplateType.VIDEO_EDITING,
            template_id=request.template_id,
            payload=payload,
            source_evidence={
                "trend_context": trend_context,
                "video_analysis_ids": analysis_ids,
                "video_insights": [item.model_dump(mode="json") for item in insights],
                "video_analysis_failures": failures,
                "generation_mode": (
                    "REBUILD_FROM_SCRATCH"
                    if request.rebuild_from_scratch
                    else "INCREMENTAL_UPDATE"
                ),
            },
            generation_model=self.generator.model_name,
            requires_human_approval=(
                request.requires_human_approval or self.settings.database_require_human_approval
            ),
        )

    def create_candidate_from_payload(
        self,
        db: Session,
        *,
        template_type: TemplateType,
        template_id: str,
        payload: dict[str, Any],
        source_evidence: dict[str, Any],
        generation_model: str,
        requires_human_approval: bool,
    ) -> TemplateUpdateCandidate:
        base = self._latest_version(db, template_type, template_id)
        base_payload = self._version_payload(template_type, base) if base else None
        base_version = base.version if base else None
        proposed_version = (base_version or 0) + 1
        errors = self.validator.validate(
            template_type,
            payload,
            is_initial_version=base is None,
        )
        candidate = TemplateUpdateCandidate(
            id=f"tuc_{uuid4().hex[:24]}",
            template_type=template_type.value,
            template_id=template_id,
            base_version=base_version,
            proposed_version=proposed_version,
            status=(
                TemplateCandidateStatus.INVALID.value
                if errors
                else TemplateCandidateStatus.VALIDATED.value
            ),
            source_evidence=source_evidence,
            proposed_payload=payload,
            diff=_json_diff(base_payload or {}, payload),
            validation_errors=errors,
            requires_human_approval=requires_human_approval,
            generation_model=generation_model,
        )
        db.add(candidate)
        db.commit()
        db.refresh(candidate)
        if not errors and not requires_human_approval:
            candidate.status = TemplateCandidateStatus.APPROVED.value
            candidate.approved_by = "SYSTEM_AUTO"
            candidate.approval_note = "Automatic application was explicitly enabled."
            candidate.approved_at = _now()
            self._apply_candidate(db, candidate)
        return candidate

    def validate_candidate(self, db: Session, candidate_id: str) -> TemplateUpdateCandidate:
        candidate = self.get_candidate(db, candidate_id)
        if candidate.status in {
            TemplateCandidateStatus.APPLIED.value,
            TemplateCandidateStatus.REJECTED.value,
        }:
            raise TemplateKnowledgeDomainError(
                "CANDIDATE_FINALIZED",
                "A finalized candidate cannot be revalidated.",
                status_code=409,
            )
        stale = self._candidate_staleness_errors(db, candidate)
        errors = stale + self.validator.validate(
            candidate.template_type,
            candidate.proposed_payload,
            is_initial_version=candidate.base_version is None,
        )
        candidate.validation_errors = errors
        candidate.status = (
            TemplateCandidateStatus.INVALID.value
            if errors
            else TemplateCandidateStatus.VALIDATED.value
        )
        db.commit()
        db.refresh(candidate)
        return candidate

    def approve_candidate(
        self,
        db: Session,
        candidate_id: str,
        decision: CandidateDecision,
    ) -> TemplateUpdateCandidate:
        candidate = self.get_candidate(db, candidate_id)
        if candidate.status != TemplateCandidateStatus.VALIDATED.value:
            raise TemplateKnowledgeDomainError(
                "CANDIDATE_NOT_VALIDATED",
                "Only a currently VALIDATED candidate can be approved.",
                status_code=409,
            )
        stale_errors = self._candidate_staleness_errors(db, candidate)
        if stale_errors:
            candidate.validation_errors = stale_errors
            candidate.status = TemplateCandidateStatus.INVALID.value
            db.commit()
            raise TemplateKnowledgeDomainError(
                "CANDIDATE_STALE",
                "The base version changed; generate a fresh candidate.",
                status_code=409,
            )
        candidate.status = TemplateCandidateStatus.APPROVED.value
        candidate.approved_by = decision.actor
        candidate.approval_note = decision.note
        candidate.approved_at = _now()
        return self._apply_candidate(db, candidate)

    def reject_candidate(
        self,
        db: Session,
        candidate_id: str,
        decision: CandidateRejection,
    ) -> TemplateUpdateCandidate:
        candidate = self.get_candidate(db, candidate_id)
        if candidate.status in {
            TemplateCandidateStatus.APPLIED.value,
            TemplateCandidateStatus.REJECTED.value,
        }:
            raise TemplateKnowledgeDomainError(
                "CANDIDATE_FINALIZED",
                "A finalized candidate cannot be rejected.",
                status_code=409,
            )
        candidate.status = TemplateCandidateStatus.REJECTED.value
        candidate.rejected_by = decision.actor
        candidate.rejection_reason = decision.reason
        candidate.rejected_at = _now()
        db.commit()
        db.refresh(candidate)
        return candidate

    def get_candidate(self, db: Session, candidate_id: str) -> TemplateUpdateCandidate:
        candidate = db.get(TemplateUpdateCandidate, candidate_id)
        if candidate is None:
            raise TemplateKnowledgeDomainError(
                "CANDIDATE_NOT_FOUND", "Template candidate was not found.", status_code=404
            )
        return candidate

    def list_candidates(
        self,
        db: Session,
        *,
        template_type: TemplateType | None = None,
        status: TemplateCandidateStatus | None = None,
        limit: int = 100,
    ) -> list[TemplateCandidateRead]:
        query = select(TemplateUpdateCandidate).order_by(TemplateUpdateCandidate.created_at.desc())
        if template_type is not None:
            query = query.where(TemplateUpdateCandidate.template_type == template_type.value)
        if status is not None:
            query = query.where(TemplateUpdateCandidate.status == status.value)
        rows = db.scalars(query.limit(limit)).all()
        return [TemplateCandidateRead.model_validate(row) for row in rows]

    def list_versions(
        self,
        db: Session,
        *,
        template_type: TemplateType | None = None,
        status: TemplateVersionStatus | None = None,
    ) -> list[TemplateVersionRead]:
        result: list[TemplateVersionRead] = []
        if template_type in {None, TemplateType.TRADE_AREA}:
            query = select(TradeAreaDBRecord).order_by(
                TradeAreaDBRecord.template_id, TradeAreaDBRecord.version.desc()
            )
            if status is not None:
                query = query.where(TradeAreaDBRecord.status == status.value)
            for row in db.scalars(query):
                result.append(_trade_area_read(row))
        if template_type in {None, TemplateType.VIDEO_EDITING}:
            query = select(VideoEditingDBRecord).order_by(
                VideoEditingDBRecord.template_id, VideoEditingDBRecord.version.desc()
            )
            if status is not None:
                query = query.where(VideoEditingDBRecord.status == status.value)
            for row in db.scalars(query):
                result.append(_editing_read(row))
        return result

    def analyze_reference_video(
        self,
        db: Session,
        *,
        trend_id: str,
        youtube_url: str,
        trend_context: dict[str, Any],
        force: bool = False,
    ) -> TemplateVideoAnalysis:
        _validate_youtube_url(youtube_url)
        fingerprint = hashlib.sha256(
            f"{trend_id}\n{youtube_url}\n{self.video_analyzer.model_name}".encode()
        ).hexdigest()
        row = db.scalar(
            select(TemplateVideoAnalysis).where(
                TemplateVideoAnalysis.source_fingerprint == fingerprint
            )
        )
        if row is not None and row.status == "COMPLETED" and not force:
            return row
        if row is None:
            row = TemplateVideoAnalysis(
                id=f"tva_{uuid4().hex[:24]}",
                trend_id=trend_id,
                youtube_url=youtube_url,
                source_fingerprint=fingerprint,
                model=self.video_analyzer.model_name,
                status="RUNNING",
                insights={},
            )
            db.add(row)
        else:
            row.status = "RUNNING"
            row.error_message = None
        db.commit()
        try:
            insight = self.video_analyzer.analyze(
                trend_id=trend_id,
                youtube_url=youtube_url,
                trend_context=trend_context,
            )
        except TemplateKnowledgeLLMError as exc:
            row.status = "FAILED"
            row.error_message = str(exc)
            row.analyzed_at = _now()
            db.commit()
            raise _llm_domain_error(exc, code="GEMINI_VIDEO_ANALYSIS_FAILED") from exc
        row.status = "COMPLETED"
        row.insights = insight.model_dump(mode="json")
        row.error_message = None
        row.analyzed_at = _now()
        db.commit()
        db.refresh(row)
        return row

    def analyze_trade_area(
        self,
        db: Session,
        request: TradeAreaAnalyzeRequest,
    ) -> TradeAreaAnalysisRead:
        template = self._select_trade_area_db(db, request)
        content = TradeAreaDBContent.model_validate(_trade_area_payload(template))
        try:
            result = self.generator.analyze_trade_area(
                template=content,
                evidence=request.evidence,
            )
        except TemplateKnowledgeLLMError as exc:
            raise _llm_domain_error(exc) from exc
        source_ids = {item.source_id for item in request.evidence.sources}
        if not set(result.evidence_source_ids).issubset(source_ids):
            raise TemplateKnowledgeDomainError(
                "ANALYSIS_EVIDENCE_INVALID",
                "Trade-area analysis referenced an unknown evidence source.",
                status_code=502,
            )
        row = TradeAreaAnalysis(
            id=f"taa_{uuid4().hex[:24]}",
            template_id=template.template_id,
            template_version=template.version,
            evidence_snapshot=request.evidence.model_dump(mode="json"),
            result=result.model_dump(mode="json"),
        )
        db.add(row)
        db.commit()
        return TradeAreaAnalysisRead(
            analysis_id=row.id,
            template_id=row.template_id,
            template_version=row.template_version,
            result=TradeAreaAnalysisResult.model_validate(row.result),
        )

    def _apply_candidate(
        self,
        db: Session,
        candidate: TemplateUpdateCandidate,
    ) -> TemplateUpdateCandidate:
        if candidate.status != TemplateCandidateStatus.APPROVED.value:
            raise TemplateKnowledgeDomainError(
                "CANDIDATE_NOT_APPROVED", "Candidate must be approved before application."
            )
        now = _now()
        template_type = TemplateType(candidate.template_type)
        if template_type == TemplateType.TRADE_AREA:
            content = TradeAreaDBContent.model_validate(candidate.proposed_payload)
            for current in db.scalars(
                select(TradeAreaDBRecord).where(
                    TradeAreaDBRecord.template_id == candidate.template_id,
                    TradeAreaDBRecord.status == TemplateVersionStatus.ACTIVE.value,
                )
            ):
                current.status = TemplateVersionStatus.ARCHIVED.value
            db.add(
                TradeAreaDBRecord(
                    template_id=candidate.template_id,
                    version=candidate.proposed_version,
                    status=TemplateVersionStatus.ACTIVE.value,
                    name=content.name,
                    description=content.description,
                    industry_categories=content.industry_categories,
                    area_types=content.area_types,
                    analysis_dimensions=[
                        item.model_dump(mode="json") for item in content.analysis_dimensions
                    ],
                    inference_rules=[
                        item.model_dump(mode="json") for item in content.inference_rules
                    ],
                    recommendation_hints=content.recommendation_hints,
                    prompt_context=content.prompt_context,
                    policy=content.policy.model_dump(mode="json"),
                    evidence_summary=candidate.source_evidence,
                    source_candidate_id=candidate.id,
                    activated_at=now,
                )
            )
        else:
            content = VideoEditingDBContent.model_validate(candidate.proposed_payload)
            for current in db.scalars(
                select(VideoEditingDBRecord).where(
                    VideoEditingDBRecord.template_id == candidate.template_id,
                    VideoEditingDBRecord.status == TemplateVersionStatus.ACTIVE.value,
                )
            ):
                current.status = TemplateVersionStatus.ARCHIVED.value
            db.add(
                VideoEditingDBRecord(
                    template_id=candidate.template_id,
                    version=candidate.proposed_version,
                    status=TemplateVersionStatus.ACTIVE.value,
                    name=content.name,
                    recommendation_title=content.recommendation_title,
                    recommendation_concept=content.recommendation_concept,
                    recommendation_metadata=content.recommendation_metadata.model_dump(mode="json"),
                    shooting_guide=content.shooting_guide.model_dump(mode="json"),
                    editing_rules=content.editing_rules.model_dump(mode="json"),
                    trend_ids=content.trend_ids,
                    evidence_summary=candidate.source_evidence,
                    source_candidate_id=candidate.id,
                    activated_at=now,
                )
            )
        candidate.status = TemplateCandidateStatus.APPLIED.value
        candidate.applied_at = now
        db.commit()
        db.refresh(candidate)
        return candidate

    def _candidate_staleness_errors(
        self, db: Session, candidate: TemplateUpdateCandidate
    ) -> list[dict[str, str]]:
        latest = self._latest_version(
            db, TemplateType(candidate.template_type), candidate.template_id
        )
        current_version = latest.version if latest else None
        if current_version == candidate.base_version and candidate.proposed_version == (
            (current_version or 0) + 1
        ):
            return []
        return [
            {
                "code": "BASE_VERSION_STALE",
                "path": "base_version",
                "message": (
                    f"Candidate base_version={candidate.base_version} does not match "
                    f"current_version={current_version}."
                ),
            }
        ]

    def _select_trends(self, db: Session, trend_ids: list[str]) -> list[Challenge]:
        if trend_ids:
            rows = list(db.scalars(select(Challenge).where(Challenge.id.in_(trend_ids))))
            by_id = {row.id: row for row in rows}
            ordered = [by_id[item] for item in trend_ids if item in by_id]
        else:
            ordered = list(
                db.scalars(
                    select(Challenge)
                    .where(Challenge.active.is_(True))
                    .order_by(
                        Challenge.automatic_rank.asc().nullslast(),
                        Challenge.automatic_score.desc(),
                    )
                    .limit(self.settings.database_max_reference_videos * 3)
                )
            )
        return [row for row in ordered if _representative_youtube_url(row)][
            : self.settings.database_max_reference_videos
        ]

    def _select_trade_area_db(
        self, db: Session, request: TradeAreaAnalyzeRequest
    ) -> TradeAreaDBRecord:
        if request.template_id is not None:
            if request.template_version is not None:
                template = db.get(
                    TradeAreaDBRecord, (request.template_id, request.template_version)
                )
            else:
                template = db.scalar(
                    select(TradeAreaDBRecord)
                    .where(
                        TradeAreaDBRecord.template_id == request.template_id,
                        TradeAreaDBRecord.status == TemplateVersionStatus.ACTIVE.value,
                    )
                    .order_by(TradeAreaDBRecord.version.desc())
                )
            if template is None or template.status != TemplateVersionStatus.ACTIVE.value:
                raise TemplateKnowledgeDomainError(
                    "ACTIVE_TRADE_AREA_DB_NOT_FOUND",
                    "The requested ACTIVE trade-area DB version was not found.",
                    status_code=404,
                )
            return template
        templates = list(
            db.scalars(
                select(TradeAreaDBRecord).where(
                    TradeAreaDBRecord.status == TemplateVersionStatus.ACTIVE.value
                )
            )
        )
        industry = request.evidence.industry_category.casefold()
        area_type = (request.evidence.area_type or "").casefold()

        def score(item: TradeAreaDBRecord) -> tuple[int, int]:
            industries = {str(value).casefold() for value in item.industry_categories}
            areas = {str(value).casefold() for value in item.area_types}
            return (
                2 if industry in industries else 1 if "all" in industries else 0,
                2 if area_type and area_type in areas else 1 if "all" in areas else 0,
            )

        compatible = [item for item in templates if score(item) > (0, 0)]
        if not compatible:
            raise TemplateKnowledgeDomainError(
                "ACTIVE_TRADE_AREA_DB_NOT_FOUND",
                "No ACTIVE trade-area DB version matches the supplied industry and area type.",
                status_code=404,
            )
        return max(compatible, key=lambda item: (score(item), item.version))

    @staticmethod
    def _latest_trade_area(db: Session, template_id: str) -> TradeAreaDBRecord | None:
        return db.scalar(
            select(TradeAreaDBRecord)
            .where(TradeAreaDBRecord.template_id == template_id)
            .order_by(TradeAreaDBRecord.version.desc())
        )

    @staticmethod
    def _latest_editing(db: Session, template_id: str) -> VideoEditingDBRecord | None:
        return db.scalar(
            select(VideoEditingDBRecord)
            .where(VideoEditingDBRecord.template_id == template_id)
            .order_by(VideoEditingDBRecord.version.desc())
        )

    def _latest_version(
        self, db: Session, template_type: TemplateType, template_id: str
    ) -> TradeAreaDBRecord | VideoEditingDBRecord | None:
        if template_type == TemplateType.TRADE_AREA:
            return self._latest_trade_area(db, template_id)
        return self._latest_editing(db, template_id)

    @staticmethod
    def _version_payload(
        template_type: TemplateType,
        row: TradeAreaDBRecord | VideoEditingDBRecord,
    ) -> dict[str, Any]:
        if template_type == TemplateType.TRADE_AREA:
            return _trade_area_payload(row)  # type: ignore[arg-type]
        return _editing_payload(row)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def get_template_knowledge_service() -> TemplateKnowledgeService:
    return TemplateKnowledgeService()


def _trade_area_payload(row: TradeAreaDBRecord) -> dict[str, Any]:
    return {
        "name": row.name,
        "description": row.description,
        "industry_categories": row.industry_categories or [],
        "area_types": row.area_types or [],
        "analysis_dimensions": row.analysis_dimensions or [],
        "inference_rules": row.inference_rules or [],
        "recommendation_hints": row.recommendation_hints or [],
        "prompt_context": row.prompt_context,
        "policy": row.policy or {},
    }


def _editing_payload(row: VideoEditingDBRecord) -> dict[str, Any]:
    return {
        "name": row.name,
        "recommendation_title": row.recommendation_title,
        "recommendation_concept": row.recommendation_concept,
        "recommendation_metadata": row.recommendation_metadata or {},
        "shooting_guide": row.shooting_guide or {},
        "editing_rules": row.editing_rules or {},
        "trend_ids": row.trend_ids or [],
    }


def _trade_area_read(row: TradeAreaDBRecord) -> TemplateVersionRead:
    return TemplateVersionRead(
        template_type=TemplateType.TRADE_AREA,
        template_id=row.template_id,
        version=row.version,
        status=TemplateVersionStatus(row.status),
        payload=_trade_area_payload(row),
        evidence_summary=row.evidence_summary or {},
        source_candidate_id=row.source_candidate_id,
        activated_at=row.activated_at,
    )


def _editing_read(row: VideoEditingDBRecord) -> TemplateVersionRead:
    return TemplateVersionRead(
        template_type=TemplateType.VIDEO_EDITING,
        template_id=row.template_id,
        version=row.version,
        status=TemplateVersionStatus(row.status),
        payload=_editing_payload(row),
        evidence_summary=row.evidence_summary or {},
        source_candidate_id=row.source_candidate_id,
        activated_at=row.activated_at,
    )


def _json_diff(before: Any, after: Any, path: str = "$") -> list[dict[str, Any]]:
    if isinstance(before, dict) and isinstance(after, dict):
        result: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after)):
            child = f"{path}.{key}"
            if key not in before:
                result.append({"op": "ADD", "path": child, "before": None, "after": after[key]})
            elif key not in after:
                result.append({"op": "REMOVE", "path": child, "before": before[key], "after": None})
            else:
                result.extend(_json_diff(before[key], after[key], child))
        return result
    if before != after:
        return [{"op": "REPLACE", "path": path, "before": before, "after": after}]
    return []


def _trend_payload(row: Challenge) -> dict[str, Any]:
    return {
        "trend_id": row.id,
        "name": row.override_name if row.name_overridden else row.automatic_name,
        "category": row.category,
        "lifecycle": row.lifecycle,
        "korea_relevance": row.kr_affinity,
        "confidence": row.confidence,
        "representative_youtube_url": _representative_youtube_url(row),
        "representative_video_metadata": row.representative_video_metadata or {},
        "raw_details": row.raw_details or {},
    }


def _representative_youtube_url(row: Challenge) -> str | None:
    value = (
        row.override_representative_youtube_url
        if row.representative_video_overridden
        else row.automatic_representative_youtube_url
    )
    return value.strip() if isinstance(value, str) and value.strip() else None


def _validate_youtube_url(value: str) -> None:
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold()
    allowed = host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
    allowed = allowed or host.endswith(".youtube.com")
    if parsed.scheme != "https" or not allowed:
        raise TemplateKnowledgeDomainError(
            "YOUTUBE_URL_INVALID",
            "Reference video analysis accepts a public HTTPS YouTube URL only.",
        )


def _llm_domain_error(
    exc: TemplateKnowledgeLLMError,
    *,
    code: str = "DATABASE_LLM_FAILED",
) -> TemplateKnowledgeDomainError:
    return TemplateKnowledgeDomainError(
        code,
        str(exc),
        status_code=502 if exc.retryable else 503,
        retryable=exc.retryable,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)
