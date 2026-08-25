from __future__ import annotations

from app.agents.shortform.llm import ShortformLLMError
from app.agents.shortform.service import ShortformAgentService, get_shortform_agent_service
from app.agents.shortform.types import (
    DecisionPromotionSubject,
    StateUpdates,
    ShortformTurnDecision,
    VideoEditingDBCandidate,
    VideoEditingDBSelection,
)
from app.db.session import SessionLocal
from app.main import app
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.models.shortform_session import ShortformSession
from app.schemas.shortform import ShortformAction


class FakeShortformLLM:
    def decide_turn(self, **kwargs) -> ShortformTurnDecision:
        return ShortformTurnDecision(
            action=ShortformAction.CONFIRM,
            assistant_message=(
                "이렇게 이해했어요. 딸기 크림 라떼 판매를 늘리고, 10분 안에 얼굴 없이 촬영할게요."
            ),
            state_updates=StateUpdates(
                promotion_category="MENU",
                promotion_subject=DecisionPromotionSubject(
                    type="MENU",
                    name="딸기 크림 라떼",
                    menu_id="menu_001",
                    details=[],
                ),
                promotion_objective="sales",
                filming_time="within_10m",
                face_exposure="not_allowed",
                creative_preferences=[],
                secondary_information=[],
                facts_from_user=[],
            ),
            options=[],
            missing_required_fields=[],
            conflicts=[],
            ready_for_confirmation=True,
        )

    def select_video_editing_db(
        self, *, candidates: list[VideoEditingDBCandidate], **kwargs
    ) -> VideoEditingDBSelection:
        candidate = candidates[0]
        return VideoEditingDBSelection(
            candidate_key=candidate.candidate_key,
            project_title=f"{candidate.name} 프로젝트",
            title=candidate.recommendation_title,
            concept=candidate.recommendation_concept,
            internal_reason="fake contextual selection for contract test",
        )


class FailingRecommendationLLM(FakeShortformLLM):
    def select_video_editing_db(self, **kwargs) -> VideoEditingDBSelection:
        raise ShortformLLMError(
            "recommendation selector unavailable",
            status_code=503,
        )


def _store_context() -> dict:
    return {
        "store_context": {
            "store": {
                "store_id": "store_123",
                "store_name": "사릴스 카페",
                "category": "카페",
                "location": {"address": "서울 관악구 OO로 OO"},
                "atmosphere": ["아늑함", "밝음"],
                "representative_color": "#D9B38C",
                "store_photos": [],
            },
            "representative_menus": [
                {
                    "menu_id": "menu_001",
                    "name": "딸기 크림 라떼",
                    "price": 6500,
                    "currency": "KRW",
                }
            ],
            "trade_area": {
                "characteristics": ["오피스 상권"],
                "target_age_ranges": ["20대", "30대"],
            },
        }
    }


def _seed_video_editing_db(
    template_id: str,
    *,
    title: str,
    requires_face: bool = False,
    metadata_overrides: dict | None = None,
) -> None:
    with SessionLocal() as db:
        recommendation_metadata = {
            "supported_subject_types": ["MENU"],
            "supported_objectives": ["sales"],
            "supported_filming_times": ["within_10m"],
            "supported_face_modes": ["not_allowed", "allowed"],
            "minimum_filming_time": "within_5m",
            "requires_face": requires_face,
            "requires_tts": False,
            "requires_photo_input": False,
            "renderer_supported": True,
            "difficulty": "하",
        }
        recommendation_metadata.update(metadata_overrides or {})
        db.add(
            VideoEditingDBRecord(
                template_id=template_id,
                version=1,
                status="ACTIVE",
                name=title,
                recommendation_title=title,
                recommendation_concept="완성 메뉴를 먼저 보여준 뒤 핵심 제조 장면으로 이어지는 영상",
                recommendation_metadata=recommendation_metadata,
                shooting_guide={
                    "estimated_shooting_sec": 480,
                    "difficulty": "하",
                    "scenes": [
                        {
                            "scene_order": 1,
                            "scene_description": "완성된 메뉴를 화면 중앙에 보여준다",
                            "scene_dialogue": None,
                            "scene_subtitle": "요즘 가장 많이 찾는 메뉴",
                            "shot_type": "클로즈업",
                            "target_duration_sec": 3,
                        }
                    ],
                    "tasks": [
                        {
                            "task_order": 1,
                            "description": "완성된 메뉴를 화면 중앙에 촬영합니다.",
                        }
                    ],
                },
                editing_rules={},
                trend_ids=[],
            )
        )
        db.commit()


def test_shortform_agent_one_at_a_time_flow(client, auth_headers):
    _seed_video_editing_db("video_editing_db_014", title="메뉴 한눈에 보여주기")
    _seed_video_editing_db("video_editing_db_028", title="제조 과정 빠르게 보여주기")
    _seed_video_editing_db("face_only", title="사장님 얼굴 인터뷰", requires_face=True)

    fake_service = ShortformAgentService(llm=FakeShortformLLM())
    app.dependency_overrides[get_shortform_agent_service] = lambda: fake_service
    try:
        created = client.post(
            "/api/v1/shortform-sessions",
            headers=auth_headers,
            json=_store_context(),
        )
        assert created.status_code == 200
        body = created.json()
        session_id = body["session_id"]
        assert body["status"] == "COLLECTING"
        assert len(body["options"]) == 2

        turn = client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={
                "input": {
                    "type": "TEXT",
                    "text": "딸기 크림 라떼 판매를 늘리고 얼굴 없이 10분 안에 찍고 싶어요",
                }
            },
        )
        assert turn.status_code == 200
        assert turn.json()["action"] == "CONFIRM"
        assert turn.json()["project_state"]["ready_for_confirmation"] is True
        assert turn.json()["recommendation"] is None

        recommend = client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "CONFIRM", "value": True}},
        )
        assert recommend.status_code == 200
        first = recommend.json()["recommendation"]
        assert recommend.json()["action"] == "RECOMMEND"
        assert first["editing_template_id"] == "video_editing_db_014"
        assert first["editing_template_id"] != "face_only"

        next_response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/recommendations/next",
            headers=auth_headers,
            json={},
        )
        assert next_response.status_code == 200
        second = next_response.json()["recommendation"]
        assert second["editing_template_id"] == "video_editing_db_028"
        assert next_response.json()["shown_template_ids"] == [
            "video_editing_db_014",
            "video_editing_db_028",
        ]

        guide = client.get(
            "/api/v1/editing-templates/video_editing_db_028/versions/1/shooting-guide",
            headers=auth_headers,
        )
        assert guide.status_code == 200
        assert guide.json()["template_id"] == "video_editing_db_028"
        assert guide.json()["scenes"][0]["scene_order"] == 1
        assert guide.json()["tasks"] == [
            {
                "display_order": 1,
                "task_title": "완성된 메뉴를 화면 중앙에 촬영합니다.",
                "scene_index": 0,
                "guide": {"instructions": ["완성된 메뉴를 화면 중앙에 촬영합니다."]},
            }
        ]
        assert "task_type" not in guide.json()["tasks"][0]
        assert "guide_type" not in guide.json()["tasks"][0]["guide"]

        deleted = client.delete(
            f"/api/v1/shortform-sessions/{session_id}",
            headers=auth_headers,
        )
        assert deleted.status_code == 204
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shortform_session_requires_internal_api_key(client):
    response = client.post("/api/v1/shortform-sessions", json=_store_context())
    assert response.status_code == 401


def test_openapi_preserves_live_legacy_backend_contract(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/editing-templates/{template_id}/versions/{version}/shooting-guide" in paths
    assert not any(path.startswith("/api/v1/video-editing-db/") for path in paths)

    components = schema["components"]["schemas"]
    for name in ("ShortformRecommendation", "SelectedShortform", "EditRecipe"):
        properties = components[name]["properties"]
        assert "editing_template_id" in properties
        assert "editing_template_version" in properties
        assert "video_editing_db_id" not in properties
        assert "video_editing_db_version" not in properties


def test_next_recommendation_recycles_only_record_when_no_alternative(client, auth_headers):
    _seed_video_editing_db("only_db_record", title="유일한 호환 DB 버전")

    fake_service = ShortformAgentService(llm=FakeShortformLLM())
    app.dependency_overrides[get_shortform_agent_service] = lambda: fake_service
    try:
        created = client.post(
            "/api/v1/shortform-sessions",
            headers=auth_headers,
            json=_store_context(),
        )
        session_id = created.json()["session_id"]
        client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "TEXT", "text": "메뉴를 10분 안에 얼굴 없이 홍보"}},
        )
        recommend = client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "CONFIRM", "value": True}},
        )
        assert recommend.status_code == 200
        recommendation_id = recommend.json()["recommendation"]["recommendation_id"]

        next_response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/recommendations/next",
            headers=auth_headers,
            json={},
        )
        assert next_response.status_code == 200
        replacement = next_response.json()["recommendation"]
        assert replacement["editing_template_id"] == "only_db_record"
        assert replacement["recommendation_id"] != recommendation_id

        with SessionLocal() as db:
            session = db.get(ShortformSession, session_id)
            assert session is not None
            assert session.status == "WAITING_RECOMMENDATION_ACTION"
            assert (
                session.current_recommendation["recommendation_id"]
                == replacement["recommendation_id"]
            )
            assert session.shown_video_editing_db_ids == ["only_db_record"]
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shortform_recommendation_bootstraps_packaged_database(client, auth_headers):
    fake_service = ShortformAgentService(llm=FakeShortformLLM())
    app.dependency_overrides[get_shortform_agent_service] = lambda: fake_service
    try:
        created = client.post(
            "/api/v1/shortform-sessions",
            headers=auth_headers,
            json=_store_context(),
        )
        session_id = created.json()["session_id"]
        client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "TEXT", "text": "메뉴를 10분 안에 얼굴 없이 홍보"}},
        )
        response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "CONFIRM", "value": True}},
        )
        assert response.status_code == 200
        assert response.json()["action"] == "RECOMMEND"
        assert response.json()["recommendation"]["editing_template_id"] in {
            "gt_cafe_recommendation_reels",
            "gt_jujutsu_transition",
            "gt_otsukare_summer",
        }
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shortform_recommends_even_when_every_constraint_mismatches(client, auth_headers):
    _seed_video_editing_db(
        "face_required_long_template",
        title="조건과 맞지 않지만 사용할 수 있는 영상",
        requires_face=True,
        metadata_overrides={
            "supported_subject_types": ["STORE"],
            "supported_objectives": ["trust"],
            "supported_filming_times": ["30m_plus"],
            "supported_face_modes": ["allowed"],
            "minimum_filming_time": "30m_plus",
        },
    )

    fake_service = ShortformAgentService(llm=FakeShortformLLM())
    app.dependency_overrides[get_shortform_agent_service] = lambda: fake_service
    try:
        created = client.post(
            "/api/v1/shortform-sessions",
            headers=auth_headers,
            json=_store_context(),
        )
        session_id = created.json()["session_id"]
        client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "TEXT", "text": "얼굴 없이 빠르게 메뉴 홍보"}},
        )
        response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "CONFIRM", "value": True}},
        )

        assert response.status_code == 200
        assert response.json()["action"] == "RECOMMEND"
        assert (
            response.json()["recommendation"]["editing_template_id"]
            == "face_required_long_template"
        )
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shortform_recommendation_uses_stable_fallback_when_selector_fails(client, auth_headers):
    _seed_video_editing_db("fallback_template", title="항상 반환되는 추천")

    fake_service = ShortformAgentService(llm=FailingRecommendationLLM())
    app.dependency_overrides[get_shortform_agent_service] = lambda: fake_service
    try:
        created = client.post(
            "/api/v1/shortform-sessions",
            headers=auth_headers,
            json=_store_context(),
        )
        session_id = created.json()["session_id"]
        client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "TEXT", "text": "메뉴를 홍보하고 싶어요"}},
        )
        response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "CONFIRM", "value": True}},
        )

        assert response.status_code == 200
        recommendation = response.json()["recommendation"]
        assert recommendation["editing_template_id"] == "fallback_template"
        assert recommendation["title"] == "항상 반환되는 추천"
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)
