"""SARILS 영상 편집 엔진 — 데이터 계약 (Pydantic v2).

원칙 (구현 문서 5.2·5.3):
- 모든 모델 출력은 구조화 응답. 자연어는 evidence 필드에만.
- 엔진 입력은 검증된 JSON, 출력은 MP4 + Manifest.
- 오버레이 타임코드는 '결합된 실제 제작 영상(produced video)' 기준 ms.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator


# ── enums ────────────────────────────────────────────────────────────
class ExecutionMode(str, Enum):
    CUT_ASSEMBLY = "CUT_ASSEMBLY"
    FINAL_RENDER = "FINAL_RENDER"


class SourceMode(str, Enum):
    ONE_TAKE_PASSTHROUGH = "ONE_TAKE_PASSTHROUGH"
    MULTI_CUT_ASSEMBLED = "MULTI_CUT_ASSEMBLED"


class OriginalAudioPolicy(str, Enum):
    REMOVE = "REMOVE"          # 확정: 촬영 원음은 항상 제거


class BgmPolicy(str, Enum):
    NONE = "NONE"              # 확정: BGM 합성 금지


class FinalAudioPolicy(str, Enum):
    SILENT = "SILENT"
    SFX_ONLY = "SFX_ONLY"


class OverlayType(str, Enum):
    CAPTION = "CAPTION"
    TEXT_2D = "TEXT_2D"
    SFX = "SFX"


class CropMode(str, Enum):
    KEEP = "KEEP"
    CENTER_9_16 = "CENTER_9_16"


class ColorTone(str, Enum):
    NATURAL = "NATURAL"
    WARM = "WARM"
    COOL = "COOL"
    VIVID = "VIVID"


class TransitionId(str, Enum):
    NONE = "NONE"
    HARD_CUT = "HARD_CUT"
    FLASH_WHITE = "FLASH_WHITE"   # 세그먼트 시작 2~3프레임 화이트 플래시


class FontWeight(str, Enum):
    REGULAR = "REGULAR"
    SEMIBOLD = "SEMIBOLD"
    BOLD = "BOLD"


class PlacementId(str, Enum):
    AUTO_SAFE = "AUTO_SAFE"    # Subtitle Layout Engine이 밴드 자동 선택
    BOTTOM_SAFE = "BOTTOM_SAFE"
    MID_SAFE = "MID_SAFE"
    UPPER_SAFE = "UPPER_SAFE"


class MotionId(str, Enum):
    NONE = "NONE"
    POP = "POP"
    FADE = "FADE"


class SfxStrength(str, Enum):
    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    STRONG = "STRONG"


class CutDecision(str, Enum):
    KEEP = "KEEP"
    TRIM = "TRIM"
    KEEP_FULL_CUT = "KEEP_FULL_CUT"


class QcStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


# ── 미디어 참조 ───────────────────────────────────────────────────────
class MediaFileRef(BaseModel):
    file_id: str
    path: str                          # 엔진 내부 로컬 경로 (모델에는 절대 노출하지 않음)
    sha256: str = ""
    duration_ms: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0


# ── FINAL_RENDER 입력: EditRecipe ────────────────────────────────────
class EffectApplication(BaseModel):
    effect_id: str
    params: dict = Field(default_factory=dict)


class RecipeSegment(BaseModel):
    recipe_segment_id: str
    produced_segment_id: str
    sequence_index: int                # 1부터 단조 증가 — 재배열 금지
    trim_in_ms: int
    trim_out_ms: int
    speed_multiplier: float = 1.0
    crop_mode: CropMode = CropMode.KEEP
    color_tone: ColorTone = ColorTone.NATURAL
    transition_id: TransitionId = TransitionId.NONE
    effects: list[EffectApplication] = Field(default_factory=list)
    actual_video_evidence: str = ""
    flow_preserved: bool = True

    @model_validator(mode="after")
    def _order(self):
        if self.trim_in_ms >= self.trim_out_ms:
            raise ValueError(f"{self.recipe_segment_id}: trim_in >= trim_out")
        return self


class Overlay(BaseModel):
    overlay_id: str
    produced_segment_id: str           # 어느 실제 영상 구간에 근거하는가
    overlay_type: OverlayType
    text_content: str = ""             # CAPTION/TEXT_2D
    style_id: str = "CAPTION"          # registry의 style_ids 중 하나
    start_ms: int                      # produced video 기준
    end_ms: int
    placement_id: PlacementId = PlacementId.AUTO_SAFE
    motion_id: MotionId = MotionId.NONE
    font_asset_id: str = "PRETENDARD"
    font_weight: FontWeight = FontWeight.SEMIBOLD
    # SFX 전용
    sfx_intent_id: str = ""
    sfx_strength: SfxStrength = SfxStrength.LIGHT
    audio_volume_db: float = -15.0
    # provenance
    actual_video_evidence: str = ""
    system_added: bool = True

    @model_validator(mode="after")
    def _check(self):
        if self.start_ms >= self.end_ms:
            raise ValueError(f"{self.overlay_id}: start >= end")
        if self.overlay_type in (OverlayType.CAPTION, OverlayType.TEXT_2D) and not self.text_content.strip():
            raise ValueError(f"{self.overlay_id}: 텍스트 오버레이에 문구 없음")
        if self.overlay_type == OverlayType.SFX and not self.sfx_intent_id:
            raise ValueError(f"{self.overlay_id}: SFX에 intent 없음")
        return self


class EditRecipe(BaseModel):
    recipe_id: str
    recipe_version: int = 1
    recipe_schema_version: str = "video-edit-decision-1.0"
    produced_video_id: str
    flow_preserved: bool = True        # 필수 true — Validator가 강제
    segments: list[RecipeSegment]
    overlays: list[Overlay] = Field(default_factory=list)
    original_audio_policy: OriginalAudioPolicy = OriginalAudioPolicy.REMOVE
    bgm_policy: BgmPolicy = BgmPolicy.NONE
    final_audio_policy: FinalAudioPolicy = FinalAudioPolicy.SILENT
    font_asset_id: str = "PRETENDARD"
    render_profile_id: str = "INSTAGRAM_REELS_V1"
    safe_area_profile_id: str = "INSTAGRAM_REELS_2026_V1"
    audio_mix_policy_id: str = "SILENT_V1"
    thumbnail_source_ms: int = 0


class FinalRenderRequest(BaseModel):
    job_id: str
    execution_mode: ExecutionMode = ExecutionMode.FINAL_RENDER
    idempotency_key: str = ""
    produced_video: MediaFileRef
    source_mode: SourceMode
    edit_recipe: EditRecipe
    template_bundle_id: str = "tb_local_dev_001"


# ── CUT_ASSEMBLY 입력 ────────────────────────────────────────────────
class GuideSegmentRef(BaseModel):
    guide_template_segment_id: str
    guide_sequence_index: int
    start_ms: int
    end_ms: int
    required_for_challenge: bool = True
    scene_summary: str = ""


class RawCut(BaseModel):
    raw_cut_file_id: str
    capture_sequence_index: int
    file: MediaFileRef


class CutAssemblyPolicies(BaseModel):
    reorder_allowed: bool = False
    major_segment_deletion_allowed: bool = False
    edge_trim_allowed: bool = True
    low_confidence_fallback: str = "KEEP_FULL_CUT"


class CutAssemblyRequest(BaseModel):
    job_id: str
    execution_mode: ExecutionMode = ExecutionMode.CUT_ASSEMBLY
    idempotency_key: str = ""
    shoot_session_id: str
    guide_template_id: str = ""
    guide_template_version: int = 1
    flow_lock: bool = True
    guide_segments: list[GuideSegmentRef] = Field(default_factory=list)
    raw_cuts: list[RawCut]
    policies: CutAssemblyPolicies = Field(default_factory=CutAssemblyPolicies)
    output_profile_id: str = "INTERMEDIATE_VERTICAL_V1"
    template_bundle_id: str = "tb_local_dev_001"


# ── 산출 Manifest ────────────────────────────────────────────────────
class CutItemDecision(BaseModel):
    raw_cut_file_id: str
    capture_sequence_index: int
    decision: CutDecision
    trim_in_ms: int
    trim_out_ms: int
    output_sequence_index: int
    mapped_guide_segment_id: str = ""
    confidence: float
    decision_reason: str = ""


class CutManifest(BaseModel):
    cut_manifest_id: str
    job_id: str
    shoot_session_id: str
    flow_preserved: bool
    items: list[CutItemDecision]
    assembled_file: MediaFileRef
    edit_engine_version: str
    segmenter_runs: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    qc_status: QcStatus


class QcCheck(BaseModel):
    check_id: str
    status: QcStatus
    detail: str = ""
    value: Optional[str] = None


class QcReport(BaseModel):
    status: QcStatus
    checks: list[QcCheck]

    @staticmethod
    def summarize(checks: list[QcCheck]) -> "QcReport":
        worst = QcStatus.PASS
        for c in checks:
            if c.status == QcStatus.FAIL:
                worst = QcStatus.FAIL
                break
            if c.status == QcStatus.WARN:
                worst = QcStatus.WARN
        return QcReport(status=worst, checks=checks)


class RenderManifest(BaseModel):
    render_manifest_id: str
    job_id: str
    recipe_id: str
    recipe_version: int
    recipe_hash: str
    input_video_sha256: str
    output_file: MediaFileRef
    expected_duration_ms: int
    concat_order: list[str]            # recipe_segment_id 순서 — 순서 보존 증빙
    sfx_windows_ms: list[tuple[int, int]] = Field(default_factory=list)
    sfx_assets: list[dict] = Field(default_factory=list)
    versions: dict = Field(default_factory=dict)
    ffmpeg_cmd_sha256: str = ""


class EngineResult(BaseModel):
    job_id: str
    execution_mode: ExecutionMode
    status: str                        # COMPLETED | FAILED | BLOCKED
    deliverable: bool = False          # QC PASS일 때만 true
    error: str = ""
    cut_manifest: Optional[CutManifest] = None
    render_manifest: Optional[RenderManifest] = None
    qc: Optional[QcReport] = None


# ── Avoid Map (SAM/Face/OCR 어댑터 공통 출력) ─────────────────────────
class AvoidRegion(BaseModel):
    x: int; y: int; w: int; h: int
    priority: int = 50                 # FACE=100 … EMPTY_WALL=0
    label: str = ""
    start_ms: int = 0
    end_ms: int = 10 ** 9


class AvoidMap(BaseModel):
    regions: list[AvoidRegion] = Field(default_factory=list)
