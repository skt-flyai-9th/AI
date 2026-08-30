from __future__ import annotations

from app.agents.shortform.llm import ShortformLLMError
from app.agents.shortform.service import ShortformAgentService, get_shortform_agent_service
from app.agents.shortform.types import (
    DecisionOption,
    DecisionPromotionSubject,
    StateUpdates,
    ShortformTurnDecision,
    VideoEditingDBCandidate,
    VideoEditingDBSelection,
    VideoEditingDBSelections,
)
from app.db.session import SessionLocal
from app.main import app
from app.models.challenge import Challenge
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.models.shortform_session import ShortformSession
from app.schemas.shortform import PromotionCategory, ShortformAction
from app.schemas.template_knowledge import MAX_SHOOTING_GUIDE_TITLE_CHARS
from app.template_knowledge.seeds import seed_template_library


class FakeShortformLLM:
    def decide_turn(self, **kwargs) -> ShortformTurnDecision:
        return ShortformTurnDecision(
            action=ShortformAction.CONFIRM,
            assistant_message=(
                "이렇게 이해했어요. 딸기 크림 라떼 판매를 늘리고, 10분 안에 얼굴 없이 촬영할게요."
            ),
            state_updates=StateUpdates(
                promotion_category="menu",
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
    ) -> VideoEditingDBSelections:
        return VideoEditingDBSelections(
            selections=[
                VideoEditingDBSelection(
                    candidate_key=candidate.candidate_key,
                    project_title=f"{candidate.name} 프로젝트",
                    title=f"개인화 제목 {index}",
                    concept=candidate.recommendation_concept,
                    internal_reason="fake contextual selection for contract test",
                )
                for index, candidate in enumerate(candidates[:3], start=1)
            ]
        )


class FailingRecommendationLLM(FakeShortformLLM):
    def select_video_editing_db(self, **kwargs) -> VideoEditingDBSelections:
        raise ShortformLLMError(
            "recommendation selector unavailable",
            status_code=503,
        )


class MultiQuestionLLM(FakeShortformLLM):
    def decide_turn(self, **kwargs) -> ShortformTurnDecision:
        return ShortformTurnDecision(
            action=ShortformAction.ASK,
            assistant_message=(
                "메뉴 홍보를 원하시는군요. 어떤 메뉴를 홍보할까요? 촬영 시간은 얼마나 되나요?"
            ),
            state_updates=StateUpdates(
                promotion_category=None,
                promotion_subject=None,
                promotion_objective=None,
                filming_time=None,
                face_exposure=None,
                creative_preferences=[],
                secondary_information=[],
                facts_from_user=[],
            ),
            options=[
                DecisionOption(id="review", label="후기·신뢰·전문성"),
                DecisionOption(id="trust", label="신뢰 높이기"),
            ],
            missing_required_fields=[
                "promotion_subject",
                "promotion_objective",
                "filming_time",
                "face_exposure",
            ],
            conflicts=[],
            ready_for_confirmation=False,
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
    version: int = 1,
    trend_ids: list[str] | None = None,
    requires_face: bool = False,
    metadata_overrides: dict | None = None,
    evidence_summary: dict | None = None,
) -> None:
    with SessionLocal() as db:
        linked_trend_ids = trend_ids or [f"trend_{template_id}"]
        for trend_id in linked_trend_ids:
            if db.get(Challenge, trend_id) is None:
                db.add(
                    Challenge(
                        id=trend_id,
                        automatic_name=title,
                        automatic_representative_youtube_url=(
                            f"https://www.youtube.com/shorts/{trend_id[-11:].ljust(11, 'x')}"
                        ),
                        automatic_guide_youtube_url=(
                            f"https://www.youtube.com/shorts/{trend_id[-11:].ljust(11, 'x')}"
                        ),
                    )
                )
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
                version=version,
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
                trend_ids=linked_trend_ids,
                evidence_summary=evidence_summary or {},
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
        assert turn.json()["recommendations"] == []

        recommend = client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "CONFIRM", "value": True}},
        )
        assert recommend.status_code == 200
        recommendations = recommend.json()["recommendations"]
        assert recommend.json()["action"] == "RECOMMEND"
        assert len(recommendations) == 3
        assert {item["editing_template_id"] for item in recommendations} == {
            "video_editing_db_014",
            "video_editing_db_028",
            "face_only",
        }
        assert {item["title"] for item in recommendations} == {
            "메뉴 한눈에 보여주기",
            "제조 과정 빠르게 보여주기",
            "사장님 얼굴 인터뷰",
        }

        next_response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/recommendations/next",
            headers=auth_headers,
            json={},
        )
        assert next_response.status_code == 409

        guide = client.get(
            "/api/v1/editing-templates/video_editing_db_028/versions/1/shooting-guide",
            headers=auth_headers,
        )
        assert guide.status_code == 200
        assert guide.json()["template_id"] == "video_editing_db_028"
        assert guide.json()["estimated_shooting_sec"] == 300
        assert guide.json()["estimated_shooting_time_bucket"] == "within_5m"
        assert guide.json()["scenes"][0]["scene_order"] == 1
        task = guide.json()["tasks"][0]
        assert task["display_order"] == 1
        assert len(task["task_title"]) <= MAX_SHOOTING_GUIDE_TITLE_CHARS
        assert task["scene_index"] == 0
        assert task["guide"] == {
            "instructions": ["완성된 메뉴를 화면 중앙에 촬영합니다."],
            "start_ms": 0,
            "end_ms": 3000,
        }
        assert "task_type" not in guide.json()["tasks"][0]
        assert "guide_type" not in guide.json()["tasks"][0]["guide"]

        deleted = client.delete(
            f"/api/v1/shortform-sessions/{session_id}",
            headers=auth_headers,
        )
        assert deleted.status_code == 204
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shooting_guide_exposes_reference_video_interval_ms(client, auth_headers):
    _seed_video_editing_db(
        "dance_reference",
        title="안무 참고 영상",
        evidence_summary={
            "video_insights": [
                {
                    "segments": [
                        {
                            "sequence": 1,
                            "start_sec": 1.8,
                            "end_sec": 4.3,
                            "scene_role": "HOOK",
                        }
                    ]
                }
            ]
        },
    )

    response = client.get(
        "/api/v1/editing-templates/dance_reference/versions/1/shooting-guide",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["tasks"][0]["guide"] == {
        "instructions": ["완성된 메뉴를 화면 중앙에 촬영합니다."],
        "start_ms": 1800,
        "end_ms": 4300,
    }


def test_shortform_promotion_guide_exposes_only_v21_categories(client, auth_headers):
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
        assert [item["id"] for item in body["options"]] == [
            "PROMOTION_GUIDE",
            "FREE_INPUT",
        ]
        assert body["project_state"]["current_question"] == "오늘 어떤 영상을 찍을까요?"

        response = client.post(
            f"/api/v1/shortform-sessions/{body['session_id']}/turns",
            headers=auth_headers,
            json={"input": {"type": "OPTION", "option_id": "PROMOTION_GUIDE"}},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["assistant_message"] == "무엇을 홍보하고 싶으세요?"
        assert payload["project_state"]["entry_mode"] == "promotion_guide"
        assert [item["id"] for item in payload["options"]] == [
            "MENU",
            "SPACE",
            "EVENT",
        ]
        assert [item["label"] for item in payload["options"]] == [
            "메뉴",
            "가게 공간·분위기",
            "이벤트·혜택·할인",
        ]
        assert {item.value for item in PromotionCategory} == {"menu", "space", "event"}
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shortform_filters_removed_categories_and_stores_one_question(client, auth_headers):
    fake_service = ShortformAgentService(llm=MultiQuestionLLM())
    app.dependency_overrides[get_shortform_agent_service] = lambda: fake_service
    try:
        created = client.post(
            "/api/v1/shortform-sessions",
            headers=auth_headers,
            json=_store_context(),
        )
        session_id = created.json()["session_id"]
        response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/turns",
            headers=auth_headers,
            json={"input": {"type": "TEXT", "text": "메뉴를 홍보하고 싶어요"}},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["assistant_message"].count("?") == 1
        assert payload["project_state"]["current_question"].count("?") == 1
        assert payload["options"] == [{"id": "trust", "label": "신뢰 높이기"}]

        with SessionLocal() as db:
            session = db.get(ShortformSession, session_id)
            assert session is not None
            assert session.conversation[-1]["content"] == payload["assistant_message"]
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shortform_session_requires_internal_api_key(client):
    response = client.post("/api/v1/shortform-sessions", json=_store_context())
    assert response.status_code == 401


def test_shooting_guide_accepts_challenge_id_alias(client, auth_headers):
    _seed_video_editing_db(
        "gt_jujutsu_transition",
        title="주술회전 트랜지션",
        version=4,
        trend_ids=["jujutsu_transition"],
    )

    response = client.get(
        "/api/v1/editing-templates/jujutsu_transition/versions/1/shooting-guide",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["template_id"] == "gt_jujutsu_transition"
    assert response.json()["version"] == 4


def test_information_shooting_guide_returns_scene_linked_capture_cuts(client, auth_headers):
    _seed_video_editing_db(
        "information_template",
        title="정보형 촬영 가이드",
        metadata_overrides={"format_type": "정보형"},
    )

    response = client.get(
        "/api/v1/editing-templates/information_template/versions/1/shooting-guide",
        headers=auth_headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["format_type"] == "정보형"
    assert len(payload["scenes"]) == 1
    assert len(payload["tasks"]) == 1
    assert [item["scene_index"] for item in payload["tasks"]] == [0]


def test_openapi_preserves_live_legacy_backend_contract(client):
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/editing-templates/{template_id}/versions/{version}/shooting-guide" in paths
    assert not any(path.startswith("/api/v1/video-editing-db/") for path in paths)

    components = schema["components"]["schemas"]
    challenge_properties = components["ChallengeRead"]["properties"]
    assert "editing_template_id" in challenge_properties
    assert "editing_template_version" in challenge_properties

    for name in ("ShortformRecommendation", "SelectedShortform", "EditRecipe"):
        properties = components[name]["properties"]
        assert "editing_template_id" in properties
        assert "editing_template_version" in properties
        assert "video_editing_db_id" not in properties
        assert "video_editing_db_version" not in properties


def test_next_recommendation_reports_exhaustion_when_no_alternative(client, auth_headers):
    _seed_video_editing_db("only_db_record", title="유일한 호환 DB 버전")
    _seed_video_editing_db("second_db_record", title="두 번째 호환 DB 버전")
    _seed_video_editing_db("third_db_record", title="세 번째 호환 DB 버전")

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
        recommendation_ids = {
            item["recommendation_id"] for item in recommend.json()["recommendations"]
        }

        next_response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/recommendations/next",
            headers=auth_headers,
            json={},
        )
        assert next_response.status_code == 409
        assert next_response.json()["detail"]["code"] == "NO_MORE_SHORTFORM_RECOMMENDATIONS"

        with SessionLocal() as db:
            session = db.get(ShortformSession, session_id)
            assert session is not None
            assert session.status == "WAITING_RECOMMENDATION_ACTION"
            stored = session.current_recommendation["recommendations"]
            assert {item["recommendation_id"] for item in stored} == recommendation_ids
            assert set(session.shown_video_editing_db_ids) == {
                "only_db_record",
                "second_db_record",
                "third_db_record",
            }
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_next_recommendation_without_current_recommendation_still_serves(client, auth_headers):
    _seed_video_editing_db("recovery_db_record", title="복구 가능한 DB 버전")
    _seed_video_editing_db("recovery_db_record_two", title="복구 가능한 DB 버전 2")
    _seed_video_editing_db("recovery_db_record_three", title="복구 가능한 DB 버전 3")

    fake_service = ShortformAgentService(llm=FakeShortformLLM())
    app.dependency_overrides[get_shortform_agent_service] = lambda: fake_service
    try:
        created = client.post(
            "/api/v1/shortform-sessions",
            headers=auth_headers,
            json=_store_context(),
        )
        session_id = created.json()["session_id"]
        # Simulate a confirmed brief whose first RECOMMEND response was lost
        # before any recommendation was stored.
        with SessionLocal() as db:
            session = db.get(ShortformSession, session_id)
            state = dict(session.project_state or {})
            state["brief_confirmed"] = True
            session.project_state = state
            session.current_recommendation = None
            db.commit()

        next_response = client.post(
            f"/api/v1/shortform-sessions/{session_id}/recommendations/next",
            headers=auth_headers,
            json={},
        )
        assert next_response.status_code == 200
        recommendations = next_response.json()["recommendations"]
        assert len(recommendations) == 3
        assert {item["editing_template_id"] for item in recommendations} == {
            "recovery_db_record",
            "recovery_db_record_two",
            "recovery_db_record_three",
        }
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shortform_recommendation_bootstraps_packaged_database(client, auth_headers):
    with SessionLocal() as db:
        seed_template_library(db)
        for trend_id, title in (
            ("jujutsu_transition", "주술회전 트랜지션"),
            ("otsukare_summer_challenge", "오츠카레 썸머 챌린지"),
        ):
            db.add(
                Challenge(
                    id=trend_id,
                    automatic_name=title,
                    automatic_representative_youtube_url="https://youtu.be/dQw4w9WgXcQ",
                    automatic_guide_youtube_url="https://youtu.be/dQw4w9WgXcQ",
                )
            )
        db.commit()
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
        recommendations = response.json()["recommendations"]
        assert len(recommendations) == 2
        assert {item["editing_template_id"] for item in recommendations} == {
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
    _seed_video_editing_db("mismatch_two", title="조건 불일치 영상 2", requires_face=True)
    _seed_video_editing_db("mismatch_three", title="조건 불일치 영상 3", requires_face=True)

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
        assert len(response.json()["recommendations"]) == 3
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)


def test_shortform_recommendation_uses_stable_fallback_when_selector_fails(client, auth_headers):
    _seed_video_editing_db("fallback_template", title="항상 반환되는 추천")
    _seed_video_editing_db("fallback_template_two", title="항상 반환되는 추천 2")
    _seed_video_editing_db("fallback_template_three", title="항상 반환되는 추천 3")

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
        recommendations = response.json()["recommendations"]
        assert len(recommendations) == 3
        assert len({item["editing_template_id"] for item in recommendations}) == 3
        assert {item["title"] for item in recommendations} == {
            "항상 반환되는 추천",
            "항상 반환되는 추천 2",
            "항상 반환되는 추천 3",
        }
    finally:
        app.dependency_overrides.pop(get_shortform_agent_service, None)
