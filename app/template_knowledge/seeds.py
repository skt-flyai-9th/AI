from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.editing_template import EditingTemplate
from app.models.trade_area_template import TradeAreaTemplate
from app.schemas.template_knowledge import TemplateType
from app.template_knowledge.service import TemplateKnowledgeService


def seed_template_library(
    db: Session,
    *,
    service: TemplateKnowledgeService | None = None,
) -> dict[str, Any]:
    manager = service or TemplateKnowledgeService()
    created: list[str] = []
    skipped: list[str] = []
    for template_id, payload in TRADE_AREA_SEEDS.items():
        if db.get(TradeAreaTemplate, (template_id, 1)) is not None:
            skipped.append(f"TRADE_AREA:{template_id}")
            continue
        manager.create_candidate_from_payload(
            db,
            template_type=TemplateType.TRADE_AREA,
            template_id=template_id,
            payload=payload,
            source_evidence=_bootstrap_evidence(template_id),
            generation_model="BOOTSTRAP_BASELINE_V1",
            requires_human_approval=False,
        )
        created.append(f"TRADE_AREA:{template_id}")
    for template_id, payload in EDITING_TEMPLATE_SEEDS.items():
        if db.get(EditingTemplate, (template_id, 1)) is not None:
            skipped.append(f"VIDEO_EDITING:{template_id}")
            continue
        manager.create_candidate_from_payload(
            db,
            template_type=TemplateType.VIDEO_EDITING,
            template_id=template_id,
            payload=payload,
            source_evidence=_bootstrap_evidence(template_id),
            generation_model="BOOTSTRAP_BASELINE_V1",
            requires_human_approval=False,
        )
        created.append(f"VIDEO_EDITING:{template_id}")
    return {"created": created, "skipped": skipped}


def _bootstrap_evidence(template_id: str) -> dict[str, Any]:
    return {
        "bootstrap": {
            "source_id": "SARILS_PRODUCT_BASELINE_V1",
            "template_id": template_id,
            "note": (
                "Initial conservative product baseline. Subsequent versions require the "
                "normal evidence, diff, validation, and approval lifecycle."
            ),
        }
    }


def _trade_area(
    *,
    name: str,
    description: str,
    area_types: list[str],
    characteristics: list[str],
    hints: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "industry_categories": ["all"],
        "area_types": area_types,
        "analysis_dimensions": [
            {
                "key": "population_mix",
                "description": "시간대별 유동·생활 인구 구성과 연령대 분포",
                "evidence_keys": ["population_by_hour", "age_distribution"],
            },
            {
                "key": "visit_pattern",
                "description": "방문 집중 시간, 체류 성격, 평일·주말 차이",
                "evidence_keys": ["visits_by_hour", "weekday_weekend_ratio"],
            },
            {
                "key": "demand_signal",
                "description": "업종 수요와 소비 목적을 보여주는 집계 신호",
                "evidence_keys": ["category_spend", "search_interest", "competition_density"],
            },
        ],
        "inference_rules": [
            {
                "rule_id": "peak_time",
                "description": "집계 방문량이 뚜렷한 시간대를 핵심 노출 시간으로 분류",
                "when": {
                    "evidence_keys": ["visits_by_hour"],
                    "operator": "TOP_SHARE",
                    "threshold": 1.3,
                    "threshold_max": None,
                    "minimum_sample_size": 30,
                },
                "outputs": {
                    "characteristic_candidates": [characteristics[0]],
                    "include_top_age_ranges": 0,
                    "include_peak_time": True,
                    "caution": None,
                },
                "minimum_confidence": 0.65,
            },
            {
                "rule_id": "age_mix",
                "description": "충분한 표본의 연령대 분포만 타깃 범위로 요약",
                "when": {
                    "evidence_keys": ["age_distribution"],
                    "operator": "TOP_SHARE",
                    "threshold": 0.2,
                    "threshold_max": None,
                    "minimum_sample_size": 100,
                },
                "outputs": {
                    "characteristic_candidates": ["집계 연령 구성 특성"],
                    "include_top_age_ranges": 2,
                    "include_peak_time": False,
                    "caution": None,
                },
                "minimum_confidence": 0.7,
            },
            {
                "rule_id": "area_context",
                "description": "여러 집계 신호가 일치할 때만 상권 성격을 부여",
                "when": {
                    "evidence_keys": ["population_by_hour", "visits_by_hour", "category_spend"],
                    "operator": "AGREEING_SIGNALS",
                    "threshold": 2,
                    "threshold_max": None,
                    "minimum_sample_size": 30,
                },
                "outputs": {
                    "characteristic_candidates": characteristics,
                    "include_top_age_ranges": 0,
                    "include_peak_time": False,
                    "caution": "신호가 충돌하면 불확실성을 표시합니다.",
                },
                "minimum_confidence": 0.7,
            },
        ],
        "recommendation_hints": hints,
        "prompt_context": (
            f"{description} 입력된 출처의 집계 수치 안에서만 특성을 요약하고, "
            "표본이 부족하거나 신호가 충돌하면 불확실성을 명시한다."
        ),
        "policy": {
            "aggregate_only": True,
            "no_individual_attribute_assertions": True,
            "minimum_sample_size": 30,
            "conflicting_signals": "REPORT_UNCERTAINTY",
            "sensitive_attribute_inference": "FORBIDDEN",
        },
    }


TRADE_AREA_SEEDS: dict[str, dict[str, Any]] = {
    "trade_area_office": _trade_area(
        name="오피스 상권 분석",
        description="평일 출퇴근·점심 수요가 큰 업무지구형 상권을 분석합니다.",
        area_types=["office", "업무지구", "오피스"],
        characteristics=["평일 점심 집중형", "출퇴근 시간 유동형", "주말 수요 감소형"],
        hints=["짧은 점심 의사결정", "테이크아웃 편의", "퇴근 전 방문 동기"],
    ),
    "trade_area_residential": _trade_area(
        name="주거 상권 분석",
        description="생활 인구와 재방문 수요가 중심인 주거지형 상권을 분석합니다.",
        area_types=["residential", "주거지", "아파트"],
        characteristics=["저녁 생활 수요형", "주말 가족 방문형", "재방문 중심형"],
        hints=["동네 단골 가치", "가족·소규모 방문", "저녁 및 주말 노출"],
    ),
    "trade_area_university": _trade_area(
        name="대학가 상권 분석",
        description="학사 일정과 젊은 유동 인구 변화가 큰 대학가형 상권을 분석합니다.",
        area_types=["university", "대학가", "학교"],
        characteristics=["수업 전후 집중형", "가격 민감형", "학사 일정 변동형"],
        hints=["명확한 가격·혜택", "친구와 공유할 장면", "방학기 변동 주의"],
    ),
    "trade_area_transit": _trade_area(
        name="역세권 상권 분석",
        description="환승·통행 흐름과 짧은 체류가 핵심인 역세권형 상권을 분석합니다.",
        area_types=["transit", "역세권", "환승"],
        characteristics=["출퇴근 통행형", "짧은 체류형", "즉시 구매형"],
        hints=["위치 인지", "빠른 이용", "이동 중 포착되는 첫 장면"],
    ),
    "trade_area_tourism": _trade_area(
        name="관광 상권 분석",
        description="주말·계절·방문 경험 수요가 큰 관광지형 상권을 분석합니다.",
        area_types=["tourism", "관광지", "명소"],
        characteristics=["주말 방문 집중형", "경험 소비형", "계절 변동형"],
        hints=["장소성과 대표 경험", "사진·영상으로 이해되는 결과", "계절성 명시"],
    ),
    "trade_area_general": _trade_area(
        name="일반 상권 분석",
        description="특정 유형으로 분류되지 않은 상권을 집계 신호 중심으로 분석합니다.",
        area_types=["all"],
        characteristics=["시간대 변동형", "혼합 수요형", "검증 필요형"],
        hints=["가장 강한 집계 신호 우선", "불확실성 표시", "과도한 타깃 단정 금지"],
    ),
}


def _editing_template(
    *,
    name: str,
    title: str,
    concept: str,
    subjects: list[str],
    objectives: list[str],
    filming_times: list[str],
    minimum_time: str,
    face_modes: list[str],
    requires_face: bool,
    scenes: list[tuple[str, str, float]],
    effects: list[str] | None = None,
) -> dict[str, Any]:
    guide_scenes = [
        {
            "scene_order": index,
            "scene_role": role,
            "scene_description": description,
            "scene_dialogue": None,
            "scene_subtitle": None,
            "shot_type": "클로즈업" if index == 1 else "미디엄",
            "target_duration_sec": duration,
        }
        for index, (role, description, duration) in enumerate(scenes, start=1)
    ]
    return {
        "name": name,
        "recommendation_title": title,
        "recommendation_concept": concept,
        "recommendation_metadata": {
            "supported_subject_types": subjects,
            "supported_objectives": objectives,
            "supported_filming_times": filming_times,
            "supported_face_modes": face_modes,
            "minimum_filming_time": minimum_time,
            "requires_face": requires_face,
            "requires_tts": False,
            "requires_photo_input": False,
            "renderer_supported": True,
            "source_type": "VIDEO_ONLY",
            "difficulty": "하" if len(scenes) <= 3 else "중",
        },
        "shooting_guide": {
            "estimated_shooting_sec": max(180, int(sum(item[2] for item in scenes) * 45)),
            "difficulty": "하" if len(scenes) <= 3 else "중",
            "scenes": guide_scenes,
            "tasks": [
                {"task_order": 1, "description": "세로 화면으로 흔들림 없이 촬영합니다."},
                {"task_order": 2, "description": "각 장면 앞뒤에 1초 여유를 둡니다."},
            ],
        },
        "editing_rules": {
            "source_type": "VIDEO_ONLY",
            "render_profile_id": "INSTAGRAM_REELS_V1",
            "assembly_profile_id": "INTERMEDIATE_VERTICAL_V1",
            "safe_area_profile_id": "INSTAGRAM_REELS_2026_V1",
            "audio_policy": "SILENT_V1",
            "min_cut_duration_ms": 300,
            "max_duration_sec": 30,
            "allowed_effect_ids": effects or ["PUNCH_ZOOM", "COLOR_TONE"],
            "allowed_transition_ids": ["CUT", "HARD_CUT", "FLASH_WHITE"],
        },
        "trend_ids": [],
    }


_ALL_TIMES = ["within_5m", "within_10m", "within_20m", "30m_plus"]
_ALL_OBJECTIVES = [
    "awareness",
    "new_customer",
    "visit",
    "sales",
    "reservation_inquiry",
    "trust",
    "revisit",
]


EDITING_TEMPLATE_SEEDS: dict[str, dict[str, Any]] = {
    "edit_menu_reveal": _editing_template(
        name="메뉴 결과 먼저 공개",
        title="완성 메뉴를 첫 장면에 보여주세요",
        concept="가장 먹음직스러운 결과 컷으로 시작하고 핵심 디테일과 CTA로 마무리합니다.",
        subjects=["MENU", "PRODUCT"],
        objectives=["awareness", "new_customer", "visit", "sales"],
        filming_times=_ALL_TIMES,
        minimum_time="within_5m",
        face_modes=["allowed", "not_allowed"],
        requires_face=False,
        scenes=[
            ("HOOK", "완성 메뉴나 상품을 화면 중앙에 크게 보여줍니다.", 2.5),
            ("DETAIL", "질감과 핵심 특징이 보이는 가까운 장면을 촬영합니다.", 3.0),
            ("CTA", "매장 또는 구매 행동으로 이어지는 마지막 장면을 촬영합니다.", 2.5),
        ],
    ),
    "edit_making_process": _editing_template(
        name="제조 과정 압축",
        title="만드는 과정을 빠르게 연결해보세요",
        concept="재료·손동작·완성 순간을 순서대로 압축해 과정 자체를 신뢰와 재미로 만듭니다.",
        subjects=["MENU", "PRODUCT", "SERVICE"],
        objectives=["awareness", "sales", "trust", "revisit"],
        filming_times=["within_10m", "within_20m", "30m_plus"],
        minimum_time="within_10m",
        face_modes=["allowed", "not_allowed"],
        requires_face=False,
        scenes=[
            ("HOOK", "완성 결과를 짧게 먼저 보여줍니다.", 2.0),
            ("PROCESS_START", "첫 재료나 준비 동작을 촬영합니다.", 2.5),
            ("PROCESS_KEY", "가장 특징적인 제조 동작을 가까이 촬영합니다.", 3.0),
            ("RESULT", "완성 직후의 결과를 안정적으로 촬영합니다.", 3.0),
        ],
        effects=["PUNCH_ZOOM", "COLOR_TONE", "SMOOTH_ZOOM"],
    ),
    "edit_space_walkthrough": _editing_template(
        name="매장 공간 한 바퀴",
        title="입구부터 핵심 공간까지 보여주세요",
        concept="입구·대표 좌석·분위기 포인트를 실제 이동 순서로 연결해 방문 전 이해를 돕습니다.",
        subjects=["STORE", "SERVICE"],
        objectives=["awareness", "new_customer", "visit", "reservation_inquiry"],
        filming_times=["within_10m", "within_20m", "30m_plus"],
        minimum_time="within_10m",
        face_modes=["allowed", "not_allowed"],
        requires_face=False,
        scenes=[
            ("ENTRANCE", "간판과 입구가 함께 보이도록 촬영합니다.", 3.0),
            ("SPACE", "입구에서 대표 공간으로 천천히 이동하며 촬영합니다.", 4.0),
            ("ATMOSPHERE", "조명·좌석·장식 중 대표 분위기 포인트를 촬영합니다.", 3.0),
            ("CTA", "찾아오기 쉬운 마지막 위치 장면을 촬영합니다.", 2.5),
        ],
    ),
    "edit_offer_countdown": _editing_template(
        name="혜택 핵심 카운트다운",
        title="기간과 혜택을 짧고 명확하게",
        concept="상품 결과, 혜택, 기간 순서로 정보를 분리해 빠른 행동을 유도합니다.",
        subjects=["MENU", "PRODUCT", "SERVICE", "EVENT"],
        objectives=["visit", "sales", "reservation_inquiry", "revisit"],
        filming_times=_ALL_TIMES,
        minimum_time="within_5m",
        face_modes=["allowed", "not_allowed"],
        requires_face=False,
        scenes=[
            ("HOOK", "혜택 대상이 되는 메뉴·상품·서비스 결과를 보여줍니다.", 2.5),
            ("PROOF", "혜택 내용을 뒷받침하는 실제 대상 장면을 촬영합니다.", 3.0),
            ("CTA", "방문 또는 문의로 이어지는 마지막 장면을 촬영합니다.", 2.5),
        ],
    ),
    "edit_owner_pick": _editing_template(
        name="사장님 직접 추천",
        title="사장님이 직접 한 가지를 추천해보세요",
        concept="사장님의 짧은 등장과 실제 추천 대상 장면을 연결해 신뢰를 전달합니다. 음성은 사용하지 않고 자막으로 구성합니다.",
        subjects=["MENU", "PRODUCT", "SERVICE", "STORE"],
        objectives=["awareness", "trust", "revisit"],
        filming_times=["within_10m", "within_20m", "30m_plus"],
        minimum_time="within_10m",
        face_modes=["allowed"],
        requires_face=True,
        scenes=[
            ("OWNER_HOOK", "사장님이 추천 대상을 들고 카메라를 바라봅니다.", 2.5),
            ("DETAIL", "추천 대상의 핵심 디테일을 가까이 촬영합니다.", 3.0),
            ("RESULT", "사장님과 추천 대상이 함께 보이는 장면으로 마칩니다.", 3.0),
        ],
    ),
    "edit_service_before_after": _editing_template(
        name="서비스 전후 비교",
        title="변화를 전과 후로 분명하게",
        concept="동일한 구도에서 서비스 전후를 보여주고 실제 변화만 간결하게 강조합니다.",
        subjects=["SERVICE", "PRODUCT"],
        objectives=["new_customer", "sales", "trust", "reservation_inquiry"],
        filming_times=["within_20m", "30m_plus"],
        minimum_time="within_20m",
        face_modes=["allowed", "not_allowed"],
        requires_face=False,
        scenes=[
            ("BEFORE", "서비스 전 상태를 안정된 동일 구도로 촬영합니다.", 3.0),
            ("PROCESS", "변화를 만드는 핵심 과정을 촬영합니다.", 3.0),
            ("AFTER", "서비스 후 상태를 처음과 같은 구도로 촬영합니다.", 3.5),
            ("CTA", "예약·문의 행동으로 이어질 대상 장면을 촬영합니다.", 2.5),
        ],
    ),
}
