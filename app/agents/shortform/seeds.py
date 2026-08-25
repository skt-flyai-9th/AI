from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.editing_template import EditingTemplate


# These are the three production guide records supplied with the project.  They
# intentionally use the legacy EditingTemplate model because the deployed main
# backend still sends/reads editing_template_id and editing_template_version.
_PACKAGED_TEMPLATE_SPECS: tuple[dict[str, Any], ...] = (
    {
        "template_id": "gt_jujutsu_transition",
        "version": 2,
        "name": "주술회전 트랜지션",
        "recommendation_metadata": {
            "supported_subject_types": ["MENU", "PRODUCT"],
            "supported_objectives": ["awareness", "new_customer", "visit", "sales"],
            "supported_filming_times": ["within_10m", "within_20m", "30m_plus"],
            "supported_face_modes": ["allowed", "not_allowed"],
            "minimum_filming_time": "within_10m",
            "requires_face": False,
            "requires_tts": False,
            "requires_photo_input": False,
            "renderer_supported": True,
            "source_type": "VIDEO_ONLY",
            "difficulty": "상",
        },
        "estimated_shooting_sec": 1800,
        "max_duration_sec": 9.3,
        "segments": (
            (
                "TRANSFORM_DEMO_A",
                "첫 번째 손짓 변환 · 디저트와 음료 등장",
                "노란 상의 인물이 빈 테이블 앞에서 손짓을 만들고 모션 피크 직후 "
                "실제 디저트와 음료 2잔을 공개한 뒤 결과를 유지",
                "{{caption.hook}}",
                3,
            ),
            (
                "TRANSFORM_DEMO_B",
                "두 번째 팔 스윕 변환 · 붉은 음료 공개",
                "팔로 화면을 가로질러 스윕하고 같은 자리에서 실제 보조 메뉴가 "
                "등장한 뒤 짧게 가까워지는 구도를 유지",
                "{{promo.secondary.name}}",
                2,
            ),
            (
                "TRANSFORM_DEMO_C",
                "세 번째 양손 변환 · 피자 공개",
                "양손을 모았다가 테이블 쪽으로 여는 동작을 유지하고 손이 열리는 "
                "순간 실제 세 번째 메뉴가 나타난 뒤 결과를 충분히 보여줌",
                "{{promo.tertiary.name}}",
                4,
            ),
        ),
        "trend_ids": ["jujutsu_transition"],
    },
    {
        "template_id": "gt_otsukare_summer",
        "version": 2,
        "name": "오츠카레 썸머 챌린지",
        "recommendation_metadata": {
            "supported_subject_types": ["STORE", "SERVICE"],
            "supported_objectives": ["awareness", "new_customer", "visit", "trust"],
            "supported_filming_times": ["within_10m", "within_20m", "30m_plus"],
            "supported_face_modes": ["allowed"],
            "minimum_filming_time": "within_10m",
            "requires_face": True,
            "requires_tts": False,
            "requires_photo_input": False,
            "renderer_supported": True,
            "source_type": "VIDEO_ONLY",
            "difficulty": "중",
        },
        "estimated_shooting_sec": 600,
        "max_duration_sec": 11.5,
        "segments": (
            (
                "DANCE_OVERHEAD_HOOK",
                "오버헤드 V 포즈 훅",
                "한 명의 출연자가 오버헤드 근접 구도에서 V 포즈와 표정으로 "
                "시작하고 즉시 메인 안무로 연결",
                "{{caption.hook}}",
                1,
            ),
            (
                "DANCE_PHRASE_A",
                "와이드 안무 A → 오버헤드 포인트",
                "전신이 보이는 와이드 안무를 연속 수행한 뒤 같은 동작 흐름에서 "
                "카메라가 위쪽 가까운 포인트로 이동",
                None,
                3,
            ),
            (
                "DANCE_PHRASE_B",
                "와이드 안무 B → 오버헤드 포인트",
                "전신 와이드 안무를 이어가며 팔과 상체 제스처를 보여준 뒤 "
                "두 번째 오버헤드 포인트로 연결",
                None,
                3,
            ),
            (
                "DANCE_PHRASE_C",
                "와이드 안무 C → 마지막 오버헤드 전환",
                "세 번째 와이드 안무 프레이즈를 이어가고 카메라가 위쪽으로 "
                "이동하면서 마지막 포즈를 준비",
                None,
                4,
            ),
            (
                "DANCE_FINAL_POSE",
                "오버헤드 근접 마지막 포즈",
                "얼굴과 손동작이 가까워지는 오버헤드 포즈로 안무를 닫고 짧은 "
                "CTA를 안전 영역에 표시",
                "{{store.name}} · {{caption.cta}}",
                1,
            ),
        ),
        "trend_ids": ["otsukare_summer_challenge"],
    },
    {
        "template_id": "gt_cafe_recommendation",
        "version": 1,
        "name": "카페 추천 리뷰 릴스",
        "recommendation_metadata": {
            "supported_subject_types": ["MENU", "STORE"],
            "supported_objectives": ["awareness", "new_customer", "visit", "trust"],
            "supported_filming_times": ["within_20m", "30m_plus"],
            "supported_face_modes": ["allowed", "not_allowed"],
            "minimum_filming_time": "within_20m",
            "requires_face": False,
            "requires_tts": False,
            "requires_photo_input": False,
            "renderer_supported": True,
            "source_type": "VIDEO_ONLY",
            "difficulty": "상",
        },
        "estimated_shooting_sec": 1800,
        "max_duration_sec": 12.583,
        "segments": (
            (
                "PERSONAL_HOOK",
                "애정 카페 선언 · 제조 플래시 → 대표 메뉴",
                "짧은 제조·서빙 동작을 보여준 뒤 대표 메뉴 Hero로 전환해 "
                "1인칭 애정 추천으로 시작",
                "{{caption.hook_personal}} → {{caption.hook_invite}}",
                1,
            ),
            (
                "USP_PROOF",
                "사람 또는 손이 있는 체험 컷 → 매장만의 뷰 증거",
                "컵을 든 사람 또는 손·POV를 전경에 두고 공간을 즐기는 장면을 "
                "보여준 뒤 차별점이 보이는 와이드 뷰로 전환",
                "{{store.unique_point_a}} → {{store.unique_point_b}}",
                3,
            ),
            (
                "MENU_PROOF_PRIMARY",
                "시그니처 음료 한 장면 위 순차 메뉴 자막",
                "대표 음료를 안정적으로 유지하면서 향·맛 설명과 메뉴명을 "
                "짧은 자막 이벤트로 순차 교체",
                "{{promo.primary.flavor}} → {{promo.primary.name}}",
                3,
            ),
            (
                "MENU_PROOF_SECONDARY",
                "제조 디테일 → 디저트 또는 두 번째 메뉴 증거",
                "드립·푸어링·플레이팅 등 짧은 제조 동작을 보여준 뒤 디저트 "
                "또는 두 번째 대표 메뉴를 클로즈업",
                "{{promo.dessert.message}}",
                2,
            ),
            (
                "STORE_REVEAL_CTA",
                "공간 분위기 → 매장명 → 질문형 CTA",
                "빛과 인테리어가 보이는 공간에서 분위기를 설명하고 매장명을 "
                "공개한 뒤 마지막 음료 동작 위에 질문 CTA를 표시",
                "{{store.atmosphere}} → {{store.name}} → {{caption.cta_question}}",
                3,
            ),
            (
                "END_CARD",
                "공간 배경 위 강한 추천 엔드카드",
                "여백 있는 실제 공간 영상 위에 큰 2줄 추천 문구를 표시해 영상을 닫음",
                "{{caption.local_identity}} {{caption.endorsement}}",
                1,
            ),
        ),
        "trend_ids": ["cafe_recommendation_reels"],
    },
)


def _template_payload(spec: dict[str, Any]) -> dict[str, Any]:
    scenes: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for order, (role, summary, instruction, subtitle, duration) in enumerate(
        spec["segments"], start=1
    ):
        scenes.append(
            {
                "scene_order": order,
                "scene_role": role,
                "scene_description": f"{summary} — {instruction}",
                "scene_dialogue": None,
                "scene_subtitle": subtitle,
                "shot_type": "가이드 구간 재현",
                "target_duration_sec": duration,
            }
        )
        tasks.append(
            {
                "display_order": order,
                "task_title": summary,
                "task_type": "영상촬영",
                "shooting_scene_order": order,
                "guide": {
                    "guide_type": "OVERLAY",
                    "instructions": [instruction],
                    "broll_shot": {"distance": None, "angle": None},
                },
            }
        )

    return {
        "template_id": spec["template_id"],
        "version": spec["version"],
        "status": "ACTIVE",
        "name": spec["name"],
        "recommendation_title": spec["name"],
        "recommendation_concept": " → ".join(segment[1] for segment in spec["segments"]),
        "recommendation_metadata": spec["recommendation_metadata"],
        "shooting_guide": {
            "estimated_shooting_sec": spec["estimated_shooting_sec"],
            "difficulty": spec["recommendation_metadata"]["difficulty"],
            "scenes": scenes,
            "tasks": tasks,
        },
        "editing_rules": {
            "source_type": "VIDEO_ONLY",
            "render_profile_id": "INSTAGRAM_REELS_V1",
            "assembly_profile_id": "INTERMEDIATE_VERTICAL_V1",
            "safe_area_profile_id": "INSTAGRAM_REELS_2026_V1",
            "audio_policy": "SILENT_V1",
            "min_cut_duration_ms": 300,
            "max_duration_sec": spec["max_duration_sec"],
            "allowed_effect_ids": [],
            "allowed_transition_ids": ["CUT", "HARD_CUT"],
        },
        "trend_ids": spec["trend_ids"],
    }


PACKAGED_EDITING_TEMPLATES: tuple[dict[str, Any], ...] = tuple(
    _template_payload(spec) for spec in _PACKAGED_TEMPLATE_SPECS
)


def seed_packaged_editing_templates(db: Session) -> int:
    """Insert missing packaged templates without changing existing operator data.

    Each insert uses a savepoint so simultaneous API startups remain harmless.
    Existing rows, including rows deliberately archived by an operator, are never
    overwritten or reactivated.
    """

    added = 0
    for payload in PACKAGED_EDITING_TEMPLATES:
        key = (payload["template_id"], payload["version"])
        if db.get(EditingTemplate, key) is not None:
            continue
        try:
            with db.begin_nested():
                db.add(EditingTemplate(**payload))
                db.flush()
        except IntegrityError:
            # Another API process inserted the same immutable packaged version.
            continue
        added += 1
    db.commit()
    return added
