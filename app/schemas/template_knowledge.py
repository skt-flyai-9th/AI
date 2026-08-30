from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from app.schemas.shortform import FilmingTime


# A guide cut is an actual edit boundary, not a broad semantic chapter.
# Short references can contain more than six jump cuts while retaining the
# same subject or action, so the schema must not force those cuts to merge.
MAX_SHOOTING_GUIDE_CUTS = 60
MAX_SHOOTING_GUIDE_TITLE_CHARS = 20
_KOREAN_TEXT_PATTERN = r".*[가-힣].*"
_SHOT_TYPE_PLACEHOLDERS = {"가이드 구간 재현"}
# 이 검증은 Gemini/GPT 구조화 응답 파싱에 그대로 걸리므로, 오탐 한 번이 분석·생성
# 전체를 실패시킨다. 재현율보다 정밀도가 우선이다 — 명백한 의류·외형 용법만 잡는다.
# - 상의/하의: 앞이 한글이 아니고(어절 시작) 조사가 바로 붙는 의류 용법만 매칭.
#   "이상의/이하의/책상의/일상의/지하의"처럼 명사+의 결합은 앞 글자가 한글이라
#   제외되고, "화면 상의 자막"처럼 조사가 없는 경우도 제외된다.
# - 옷(?=[을이]): "옷을 입고"는 잡고 "옷걸이"는 통과. 화장(?!실), 헤어\s*스타일도
#   같은 원칙("화장실", "헤어지다" 오탐 방지).
_FORBIDDEN_APPEARANCE_PATTERN = re.compile(
    r"(?<![가-힣])상의(?=[를을이가는도로와과만])"
    r"|(?<![가-힣])하의(?=[를을이가는도로와과만])"
    r"|의상|복장|옷차림|(?<![가-힣])옷(?=[을이])"
    r"|헤어\s*스타일|머리\s*색|머리\s*스타일|메이크업|화장(?!실)"
    r"|남성|여성|남자|여자"
)
# 빵(?!집): "소보로빵"은 특정 메뉴라 잡고, 장소를 가리키는 "빵집"은 통과.
_REFERENCE_SPECIFIC_PRODUCT_PATTERN = re.compile(
    r"피자|케이크|빵(?!집)|페이스트리|말차|티라미수|아이스\s*커피|크레페"
)


def _validate_reusable_guide_text(value: str) -> str:
    appearance = _FORBIDDEN_APPEARANCE_PATTERN.search(value)
    if appearance is not None:
        raise ValueError(
            f"appearance detail '{appearance.group(0)}' is forbidden in reusable guide text"
        )
    product = _REFERENCE_SPECIFIC_PRODUCT_PATTERN.search(value)
    if product is not None:
        raise ValueError(
            f"reference-specific product '{product.group(0)}' must use a generic category"
        )
    return value


def _validate_shot_type(value: str) -> str:
    _validate_reusable_guide_text(value)
    if value.strip() in _SHOT_TYPE_PLACEHOLDERS:
        raise ValueError("shot_type must describe a concrete camera framing or angle")
    return value


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


class TradeAreaDBContent(BaseModel):
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
    scene_description: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "촬영 구도·카메라 움직임·피사체 배치·액션을 서술한다. 인물의 의상·헤어·메이크업 등 "
            "외형은 절대 언급하지 않는다. 음식/음료/제품은 레퍼런스 영상의 특정 메뉴명 대신 "
            "'메뉴/음료/디저트/제품' 같은 일반 카테고리로만 지칭한다."
        ),
    )
    scene_dialogue: str | None = Field(default=None, max_length=9)
    scene_subtitle: str | None = Field(default=None, max_length=200)
    shot_type: str = Field(
        min_length=1,
        max_length=80,
        pattern=_KOREAN_TEXT_PATTERN,
        description=(
            "카메라 앵글과 프레이밍을 자연스러운 한글로 서술하는 구도 필드 "
            "(예: 정면 미디엄샷, 손 클로즈업, 테이블 위 오버헤드)."
        ),
    )
    target_duration_sec: float = Field(gt=0, le=30)

    @field_validator("scene_description")
    @classmethod
    def validate_scene_description(cls, value: str) -> str:
        return _validate_reusable_guide_text(value)

    @field_validator("shot_type")
    @classmethod
    def validate_shot_type(cls, value: str) -> str:
        return _validate_shot_type(value)


class ShootingGuideTaskGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instructions: list[str] = Field(default_factory=list, max_length=20)


class ShootingGuideTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_order: int = Field(ge=1)
    task_title: str = Field(min_length=1, max_length=MAX_SHOOTING_GUIDE_TITLE_CHARS)
    scene_index: int = Field(ge=0)
    guide: ShootingGuideTaskGuide


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
    format_type: Literal["밈", "챌린지", "정보형"] = "밈"


class EditingShootingGuide(BaseModel):
    model_config = ConfigDict(extra="forbid")

    estimated_shooting_sec: int = Field(gt=0, le=7200)
    required_people: int = Field(default=1, ge=1)
    props: list[str] = Field(default_factory=list, max_length=30)
    difficulty: str
    scenes: list[ShootingGuideScene] = Field(min_length=1, max_length=MAX_SHOOTING_GUIDE_CUTS)
    tasks: list[ShootingGuideTask] = Field(max_length=MAX_SHOOTING_GUIDE_CUTS)


class VideoEditingDBRules(BaseModel):
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


class VideoEditingDBContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    recommendation_title: str = Field(min_length=1, max_length=255)
    recommendation_concept: str = Field(min_length=1, max_length=2000)
    recommendation_metadata: EditingRecommendationMetadata
    shooting_guide: EditingShootingGuide
    editing_rules: VideoEditingDBRules
    trend_ids: list[str] = Field(max_length=50)


class ReferenceVideoSegment(BaseModel):
    """One evidence-backed semantic cut from the reference video."""

    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1, le=MAX_SHOOTING_GUIDE_CUTS)
    start_sec: float = Field(ge=0, le=120)
    end_sec: float = Field(gt=0, le=120)
    scene_role: str = Field(min_length=1, max_length=80)
    description: str = Field(
        min_length=1,
        max_length=500,
        description=(
            "컷의 구도·카메라 움직임·피사체 배치·액션을 서술한다. 인물의 의상·헤어·메이크업 등 "
            "외형은 관찰했더라도 기록하지 않는다. 관찰된 음식/음료/제품은 특정 메뉴명 대신 "
            "'메뉴/음료/디저트/제품' 같은 일반 카테고리로만 서술한다."
        ),
    )
    shot_type: str = Field(
        min_length=1,
        max_length=80,
        pattern=_KOREAN_TEXT_PATTERN,
        description=(
            "카메라 앵글과 프레이밍을 자연스러운 한글로 서술하는 구도 필드 "
            "(예: 정면 미디엄샷, 손 클로즈업, 테이블 위 오버헤드)."
        ),
    )
    transition_out: str | None = Field(default=None, max_length=120)
    evidence: str = Field(min_length=1, max_length=500)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        return _validate_reusable_guide_text(value)

    @field_validator("shot_type")
    @classmethod
    def validate_shot_type(cls, value: str) -> str:
        return _validate_shot_type(value)

    @model_validator(mode="after")
    def validate_time_range(self) -> ReferenceVideoSegment:
        if self.end_sec <= self.start_sec:
            raise ValueError("end_sec must be greater than start_sec")
        return self


class EditingVideoInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trend_id: str
    youtube_url: str
    summary: str = Field(min_length=1, max_length=2000)
    hook_patterns: list[str] = Field(min_length=1, max_length=20)
    shot_sequence: list[str] = Field(min_length=1, max_length=MAX_SHOOTING_GUIDE_CUTS)
    segments: list[ReferenceVideoSegment] = Field(min_length=1, max_length=MAX_SHOOTING_GUIDE_CUTS)
    # 2026-08-30 추가 — 완성 영상 길이가 아니라 이 영상을 촬영하는 데 걸릴 실제
    # 시간을 컷 개수·복잡도 근거로 분류한다(`app/template_knowledge/llm.py`의
    # shooting_time_bucket_rules). `generate_editing()`이 이 값을 그대로
    # `recommendation_metadata.minimum_filming_time`에 반영하며, GPT가 별도로
    # 재추정하지 않는다.
    estimated_shooting_time_bucket: FilmingTime
    pacing: VideoPacing
    caption_patterns: list[str] = Field(max_length=20)
    camera_patterns: list[str] = Field(max_length=20)
    transition_patterns: list[str] = Field(max_length=20)
    audio_role: Literal["PLATFORM_MUSIC", "ORIGINAL_AMBIENCE", "NONE"]
    reusable_editing_rules: list[str] = Field(min_length=1, max_length=30)
    evidence_notes: list[str] = Field(min_length=1, max_length=30)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_segments(self) -> EditingVideoInsight:
        expected = list(range(1, len(self.segments) + 1))
        if [item.sequence for item in self.segments] != expected:
            raise ValueError("segments.sequence must be consecutive from 1")
        if len(self.shot_sequence) != len(self.segments):
            raise ValueError("shot_sequence and segments must have the same number of cuts")
        for previous, current in zip(self.segments, self.segments[1:], strict=False):
            if current.start_sec < previous.end_sec:
                raise ValueError("reference video segments must not overlap")
        return self


class VideoPacing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tempo: Literal["SLOW", "MEDIUM", "FAST", "MIXED"]
    median_cut_sec: float = Field(gt=0, le=30)
    opening_hook_sec: float = Field(gt=0, le=15)


TradeAreaInferenceRule.model_rebuild()
TradeAreaDBContent.model_rebuild()
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
    rebuild_from_scratch: bool = False


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
