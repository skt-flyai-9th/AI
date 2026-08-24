from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class TemplateType(StrEnum):
    TRADE_AREA = "TRADE_AREA"
    VIDEO_EDITING = "VIDEO_EDITING"


class TemplateVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class TemplateSourceStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class TemplateCandidateStatus(StrEnum):
    GENERATED = "GENERATED"
    VALIDATED = "VALIDATED"
    INVALID = "INVALID"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"


class TemplateKnowledgeOperation(StrEnum):
    TRADE_AREA_CANDIDATE = "TRADE_AREA_CANDIDATE"
    VIDEO_EDITING_CANDIDATE = "VIDEO_EDITING_CANDIDATE"
    TRADE_AREA_ANALYSIS = "TRADE_AREA_ANALYSIS"


class TemplateKnowledgeRunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EvidenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=160)
    source_type: str = Field(min_length=1, max_length=80)
    observed_at: datetime | None = None
    source_url: HttpUrl | None = None
    note: str = Field(default="", max_length=1000)


class TradeAreaEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    industry_category: str = Field(min_length=1, max_length=120)
    region_scope: dict[str, Any] = Field(default_factory=dict)
    area_type: str | None = Field(default=None, max_length=80)
    signals: dict[str, Any]
    sources: list[EvidenceSource] = Field(min_length=1, max_length=50)


class TradeAreaDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=1, max_length=500)
    evidence_keys: list[str] = Field(min_length=1, max_length=30)


class TradeAreaInferenceRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=1, max_length=500)
    when: TradeAreaRuleCondition
    outputs: TradeAreaRuleOutput
    minimum_confidence: float = Field(ge=0, le=1)


class TradeAreaRuleCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_keys: list[str] = Field(min_length=1, max_length=20)
    operator: Literal["GTE", "LTE", "BETWEEN", "TOP_SHARE", "AGREEING_SIGNALS"]
    threshold: float | None = None
    threshold_max: float | None = None
    minimum_sample_size: int | None = Field(default=None, ge=1)


class TradeAreaRuleOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    characteristic_candidates: list[str] = Field(min_length=1, max_length=20)
    include_top_age_ranges: int = Field(default=0, ge=0, le=5)
    include_peak_time: bool = False
    caution: str | None = Field(default=None, max_length=500)


class TradeAreaPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aggregate_only: Literal[True] = True
    no_individual_attribute_assertions: Literal[True] = True
    minimum_sample_size: int = Field(ge=1)
    conflicting_signals: Literal["REPORT_UNCERTAINTY"] = "REPORT_UNCERTAINTY"
    sensitive_attribute_inference: Literal["FORBIDDEN"] = "FORBIDDEN"


class TradeAreaTemplateContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    industry_categories: list[str] = Field(min_length=1, max_length=30)
    area_types: list[str] = Field(min_length=1, max_length=30)
    analysis_dimensions: list[TradeAreaDimension] = Field(min_length=1, max_length=30)
    inference_rules: list[TradeAreaInferenceRule] = Field(min_length=1, max_length=100)
    recommendation_hints: list[str] = Field(min_length=1, max_length=50)
    prompt_context: str = Field(min_length=1, max_length=8000)
    policy: TradeAreaPolicy


class ShootingGuideScene(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_order: int = Field(ge=1)
    scene_role: str = Field(min_length=1, max_length=80)
    scene_description: str = Field(min_length=1, max_length=500)
    scene_dialogue: str | None = Field(default=None, max_length=500)
    scene_subtitle: str | None = Field(default=None, max_length=200)
    shot_type: str = Field(min_length=1, max_length=80)
    target_duration_sec: float = Field(gt=0, le=30)


class ShootingGuideTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_order: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=500)


class EditingRecommendationMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supported_subject_types: list[str] = Field(min_length=1, max_length=30)
    supported_objectives: list[str] = Field(min_length=1, max_length=30)
    supported_filming_times: list[str] = Field(min_length=1, max_length=10)
    supported_face_modes: list[str] = Field(min_length=1, max_length=10)
    minimum_filming_time: str
    requires_face: bool
    requires_tts: Literal[False] = False
    requires_photo_input: Literal[False] = False
    renderer_supported: Literal[True] = True
    source_type: Literal["VIDEO_ONLY"] = "VIDEO_ONLY"
    difficulty: str


class EditingShootingGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimated_shooting_sec: int = Field(gt=0, le=7200)
    difficulty: str
    scenes: list[ShootingGuideScene] = Field(min_length=1, max_length=20)
    tasks: list[ShootingGuideTask] = Field(max_length=30)


class EditingTemplateRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: Literal["VIDEO_ONLY"] = "VIDEO_ONLY"
    render_profile_id: str
    assembly_profile_id: str
    safe_area_profile_id: str
    audio_policy: Literal["SILENT_V1"] = "SILENT_V1"
    min_cut_duration_ms: int = Field(ge=1)
    max_duration_sec: float = Field(gt=0, le=120)
    allowed_effect_ids: list[str] = Field(max_length=30)
    allowed_transition_ids: list[str] = Field(max_length=30)


class EditingTemplateContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    recommendation_title: str = Field(min_length=1, max_length=255)
    recommendation_concept: str = Field(min_length=1, max_length=2000)
    recommendation_metadata: EditingRecommendationMetadata
    shooting_guide: EditingShootingGuide
    editing_rules: EditingTemplateRules
    trend_ids: list[str] = Field(max_length=50)


class EditingVideoInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trend_id: str
    youtube_url: str
    summary: str = Field(min_length=1, max_length=2000)
    hook_patterns: list[str] = Field(min_length=1, max_length=20)
    shot_sequence: list[str] = Field(min_length=1, max_length=30)
    pacing: VideoPacing
    caption_patterns: list[str] = Field(max_length=20)
    camera_patterns: list[str] = Field(max_length=20)
    transition_patterns: list[str] = Field(max_length=20)
    audio_role: Literal["PLATFORM_MUSIC", "ORIGINAL_AMBIENCE", "NONE"]
    reusable_editing_rules: list[str] = Field(min_length=1, max_length=30)
    evidence_notes: list[str] = Field(min_length=1, max_length=30)
    confidence: float = Field(ge=0, le=1)


class VideoPacing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tempo: Literal["SLOW", "MEDIUM", "FAST", "MIXED"]
    median_cut_sec: float = Field(gt=0, le=30)
    opening_hook_sec: float = Field(gt=0, le=15)


TradeAreaInferenceRule.model_rebuild()
TradeAreaTemplateContent.model_rebuild()
EditingVideoInsight.model_rebuild()


class TradeAreaAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    characteristics: list[str] = Field(min_length=1, max_length=20)
    target_age_ranges: list[str] = Field(max_length=20)
    target_time_ranges: list[str] = Field(max_length=20)
    visit_purposes: list[str] = Field(max_length=20)
    opportunity_signals: list[str] = Field(max_length=20)
    cautions: list[str] = Field(max_length=20)
    evidence_source_ids: list[str] = Field(min_length=1, max_length=50)
    confidence: float = Field(ge=0, le=1)


class TradeAreaCandidateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,159}$")
    evidence: TradeAreaEvidence
    requires_human_approval: bool = True


class EditingCandidateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,159}$")
    trend_ids: list[str] = Field(default_factory=list, max_length=10)
    requires_human_approval: bool = True
    force_video_analysis: bool = False


class CandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=160)
    note: str = Field(default="", max_length=2000)


class CandidateRejection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=2000)


class TradeAreaAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence: TradeAreaEvidence
    template_id: str | None = None
    template_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_template_selector(self) -> TradeAreaAnalyzeRequest:
        if self.template_version is not None and self.template_id is None:
            raise ValueError("template_version requires template_id")
        return self


class TemplateCandidateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    template_type: TemplateType
    template_id: str
    base_version: int | None
    proposed_version: int
    status: TemplateCandidateStatus
    source_evidence: dict[str, Any]
    proposed_payload: dict[str, Any]
    diff: list[dict[str, Any]]
    validation_errors: list[dict[str, Any]]
    requires_human_approval: bool
    generation_model: str
    approved_by: str | None
    approval_note: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejection_reason: str | None
    rejected_at: datetime | None
    applied_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TradeAreaAnalysisRead(BaseModel):
    analysis_id: str
    template_id: str
    template_version: int
    result: TradeAreaAnalysisResult


class TemplateVersionRead(BaseModel):
    template_type: TemplateType
    template_id: str
    version: int
    status: TemplateVersionStatus
    payload: dict[str, Any]
    evidence_summary: dict[str, Any]
    source_candidate_id: str | None
    activated_at: datetime | None


class TemplateSourceBundleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    template_type: TemplateType
    schema_version: str
    source_filename: str
    source_sha256: str
    status: TemplateSourceStatus
    dataset_manifest: dict[str, Any]
    imported_at: datetime


class TemplateSourceRecordRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    bundle_id: str
    dataset_name: str
    record_key: str
    source_row_number: int
    status: str
    payload: dict[str, Any]


class TradeAreaSourceContextRead(BaseModel):
    bundle_id: str
    region: dict[str, Any] | None
    category: dict[str, Any] | None
    region_category_fit: dict[str, Any] | None
    official_trade_area: dict[str, Any] | None
    official_profile: dict[str, Any] | None
    mapping: dict[str, Any] | None
    source_ids: list[str]
    draft_data_included: bool


class TemplateKnowledgeRunCreateResponse(BaseModel):
    run_id: str
    operation: TemplateKnowledgeOperation
    status: TemplateKnowledgeRunStatus
    task_id: str | None = None


class TemplateKnowledgeRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    operation: TemplateKnowledgeOperation
    status: TemplateKnowledgeRunStatus
    stage: str
    progress: int
    celery_task_id: str | None
    error: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TemplateKnowledgeRunResult(BaseModel):
    run_id: str
    operation: TemplateKnowledgeOperation
    status: TemplateKnowledgeRunStatus
    result: dict[str, Any]
