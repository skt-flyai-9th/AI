from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ShortformAction(StrEnum):
    ASK = "ASK"
    SAVE_AND_ASK = "SAVE_AND_ASK"
    CLARIFY = "CLARIFY"
    SUGGEST_SWITCH = "SUGGEST_SWITCH"
    RESOLVE_CONFLICT = "RESOLVE_CONFLICT"
    CONFIRM = "CONFIRM"
    RECOMMEND = "RECOMMEND"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class ShortformSessionStatus(StrEnum):
    COLLECTING = "COLLECTING"
    CONFIRMING = "CONFIRMING"
    RECOMMENDING = "RECOMMENDING"
    WAITING_RECOMMENDATION_ACTION = "WAITING_RECOMMENDATION_ACTION"
    COMPLETED = "COMPLETED"


class TurnInputType(StrEnum):
    TEXT = "TEXT"
    OPTION = "OPTION"
    CONFIRM = "CONFIRM"


class ShortformEntryMode(StrEnum):
    PROMOTION_GUIDE = "promotion_guide"
    FREE_INPUT = "free_input"


class PromotionCategory(StrEnum):
    MENU = "menu"
    SPACE = "space"
    EVENT = "event"


class PromotionObjective(StrEnum):
    AWARENESS = "awareness"
    NEW_CUSTOMER = "new_customer"
    VISIT = "visit"
    SALES = "sales"
    RESERVATION_INQUIRY = "reservation_inquiry"
    TRUST = "trust"
    REVISIT = "revisit"


class FilmingTime(StrEnum):
    WITHIN_5M = "within_5m"
    WITHIN_10M = "within_10m"
    WITHIN_20M = "within_20m"
    PLUS_30M = "30m_plus"


# 버킷 → 예상 촬영 소요시간(분), 2026-08-30 추가. Gemini가 레퍼런스 영상을 처음
# 분석할 때 컷 개수·복잡도를 근거로 이 버킷 중 하나로 분류하고(`app/template_
# knowledge/llm.py`), `/shooting-guide` 응답은 그 버킷의 분 값(5/10/20/30)을
# 그대로 내려준다. "완성 영상 길이 × 10초" 같은 근거 없는 근사식을 대체하며,
# 화면에도 초가 아니라 이 분 단위(5분/10분/20분/30분+)로 노출된다.
FILMING_TIME_BUCKET_MINUTES: dict[str, int] = {
    FilmingTime.WITHIN_5M.value: 5,
    FilmingTime.WITHIN_10M.value: 10,
    FilmingTime.WITHIN_20M.value: 20,
    FilmingTime.PLUS_30M.value: 30,
}


class FaceExposure(StrEnum):
    ALLOWED = "allowed"
    NOT_ALLOWED = "not_allowed"


class PromotionSubject(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str | None = None
    name: str | None = None
    menu_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class StorePhoto(BaseModel):
    model_config = ConfigDict(extra="allow")

    asset_id: str
    asset_url: str | None = None


class StoreLocation(BaseModel):
    model_config = ConfigDict(extra="allow")

    address: str | None = None


class StoreInfo(BaseModel):
    model_config = ConfigDict(extra="allow")

    store_id: str
    store_name: str | None = None
    category: str | None = None
    location: StoreLocation | None = None
    atmosphere: list[str] = Field(default_factory=list)
    representative_color: str | None = None
    store_photos: list[StorePhoto] = Field(default_factory=list)


class RepresentativeMenu(BaseModel):
    model_config = ConfigDict(extra="allow")

    menu_id: str
    name: str
    price: int | float | None = None
    currency: str = "KRW"


class TradeAreaContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    characteristics: list[str] = Field(default_factory=list)
    target_age_ranges: list[str] = Field(default_factory=list)


class StoreContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    store: StoreInfo
    representative_menus: list[RepresentativeMenu] = Field(default_factory=list)
    trade_area: TradeAreaContext | dict[str, Any] | None = None


class ShortformSessionCreateRequest(BaseModel):
    store_context: StoreContext


class ShortformOption(BaseModel):
    id: str
    label: str


class ShortformProjectState(BaseModel):
    entry_mode: ShortformEntryMode | None = None
    promotion_category: PromotionCategory | None = None
    promotion_subject: PromotionSubject | None = None
    promotion_objective: PromotionObjective | None = None
    filming_time: FilmingTime | None = None
    face_exposure: FaceExposure | None = None
    creative_preferences: list[str] = Field(default_factory=list)
    secondary_information: list[str] = Field(default_factory=list)
    facts_from_user: dict[str, str] = Field(default_factory=dict)
    store_context_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    missing_required_fields: list[str] = Field(default_factory=list)
    current_question: str | None = None
    ready_for_confirmation: bool = False
    ready_for_recommendation: bool = False
    brief_confirmed: bool = False


class ShortformSessionCreateResponse(BaseModel):
    session_id: str
    status: ShortformSessionStatus
    assistant_message: str
    options: list[ShortformOption]
    project_state: ShortformProjectState


class ShortformTurnInput(BaseModel):
    type: TurnInputType
    text: str | None = None
    option_id: str | None = None
    value: bool | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ShortformTurnInput:
        if self.type == TurnInputType.TEXT and not (self.text or "").strip():
            raise ValueError("TEXT input requires text")
        if self.type == TurnInputType.OPTION and not (self.option_id or "").strip():
            raise ValueError("OPTION input requires option_id")
        if self.type == TurnInputType.CONFIRM and self.value is None:
            raise ValueError("CONFIRM input requires value")
        return self


class ShortformTurnRequest(BaseModel):
    input: ShortformTurnInput


class ShortformRecommendation(BaseModel):
    recommendation_id: str
    project_title: str
    title: str
    concept: str
    editing_template_id: str
    editing_template_version: int
    reference_url: str
    guide_video_url: str
    source_platform: str = "YOUTUBE"


class ShortformTurnResponse(BaseModel):
    session_id: str
    action: ShortformAction
    assistant_message: str | None = None
    project_state: ShortformProjectState
    options: list[ShortformOption] = Field(default_factory=list)
    recommendations: list[ShortformRecommendation] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_recommendation_batch(self) -> ShortformTurnResponse:
        if self.action == ShortformAction.RECOMMEND:
            template_ids = [item.editing_template_id for item in self.recommendations]
            if len(template_ids) != 3 or len(set(template_ids)) != 3:
                raise ValueError("RECOMMEND responses require three distinct templates")
        return self


class NextRecommendationResponse(BaseModel):
    session_id: str
    recommendations: list[ShortformRecommendation] = Field(min_length=3, max_length=3)
    shown_template_ids: list[str]


class ShootingGuideResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    template_id: str
    version: int
    # ⚠️ 2026-08-30부터 **분 단위**다(5/10/20/30 중 하나) — 예전엔 초 단위였다.
    # Gemini가 분류한 촬영 시간 버킷을 `FILMING_TIME_BUCKET_MINUTES`로 환산한
    # 값이며, 화면도 이 값을 "5분/10분/20분/30분+"로 그대로 노출한다. 필드명은
    # 백엔드가 이미 이 키(`estimated_shooting_sec`)를 읽어 저장하고 있어 계약을
    # 깨지 않으려고 그대로 둔다 — 백엔드 수정 없이 값의 단위만 바뀐다.
    estimated_shooting_sec: int = Field(ge=1)
    # 2026-08-30 추가 — Gemini가 분류한 촬영 시간 버킷(`FilmingTime`과 같은 값
    # 집합이라 백엔드가 사용자의 `filming_time` 응답과 직접 비교할 수 있다).
    # 이 필드가 생기기 전 템플릿(레거시)은 null이다.
    estimated_shooting_time_bucket: str | None = None
    required_people: int = Field(ge=1)
    props: list[str] = Field(default_factory=list)
    difficulty: str
    format_type: str = "밈"
    scenes: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
