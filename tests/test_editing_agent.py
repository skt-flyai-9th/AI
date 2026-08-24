from __future__ import annotations

from dataclasses import dataclass

from app.agents.editing.service import EditingAgentService
from app.agents.editing.types import EditingPlanDecision, VideoContext, VideoKeyframe
from app.db.session import SessionLocal
from app.models.editing_run import EditingRun
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.schemas.editing import (
    EditRecipe,
    EditingRenderResult,
    EditingRevisionRequest,
    EditingRunCreateRequest,
    EditingRunStatus,
    PublishingResult,
)


def _request() -> EditingRunCreateRequest:
    return EditingRunCreateRequest.model_validate(
        {
            "project": {
                "project_id": "project_123",
                "store_id": "store_123",
                "promotion_subject": {
                    "type": "MENU",
                    "name": "딸기 크림 라떼",
                    "menu_id": "menu_001",
                },
                "promotion_objective": "sales",
                "face_exposure": "not_allowed",
            },
            "selected_shortform": {
                "recommendation_id": "rec_123",
                "video_editing_db_id": "video_editing_db_014",
                "video_editing_db_version": 3,
            },
            "videos": [
                {
                    "video_id": "take_501",
                    "footage_url": "https://cdn.example/take-501.mp4",
                    "shooting_scene_order": 1,
                },
                {
                    "video_id": "take_502",
                    "footage_url": "https://cdn.example/take-502.mp4",
                    "shooting_scene_order": 2,
                },
            ],
            "revision": None,
        }
    )


def _recipe(*, invalid_timeline: bool = False) -> EditRecipe:
    return EditRecipe.model_validate(
        {
            "recipe_version": 1,
            "video_editing_db_id": "video_editing_db_014",
            "video_editing_db_version": 3,
            "source_type": "VIDEO_ONLY",
            "timeline": [
                {
                    "clip_order": 1,
                    "video_id": "take_501",
                    "source_start_ms": 500,
                    "source_end_ms": 2500,
                    "timeline_start_ms": 100 if invalid_timeline else 0,
                    "speed": 1.0,
                    "crop_mode": "SUBJECT_CENTER",
                    "transition_in": None,
                    "transition_out": "CUT",
                    "caption": {
                        "text": "오늘만 딸기 크림 라떼",
                        "start_ms": 0,
                        "end_ms": 1500,
                        "position": "BOTTOM",
                        "style_id": "CAPTION",
                        "font_weight": "SEMIBOLD",
                        "scale": 1.0,
                    },
                    "effects": [],
                },
                {
                    "clip_order": 2,
                    "video_id": "take_502",
                    "source_start_ms": 1000,
                    "source_end_ms": 3000,
                    "timeline_start_ms": 2000,
                    "speed": 1.0,
                    "crop_mode": "SUBJECT_CENTER",
                    "transition_in": None,
                    "transition_out": "CUT",
                    "caption": None,
                    "effects": [],
                },
            ],
            "cta": {"text": "지금 매장에서 만나보세요"},
        }
    )


def _publishing() -> PublishingResult:
    return PublishingResult(
        caption="딸기 크림 라떼를 만나보세요.",
        hashtags=["#딸기라떼", "#카페신메뉴"],
        post_note="음원은 게시 시 플랫폼 내에서 추가해주세요.",
    )


class FakeVideoContextBuilder:
    def build(self, videos):
        return [
            VideoContext(
                video_id=video.video_id,
                shooting_scene_order=video.shooting_scene_order,
                duration_ms=5000,
                width=1080,
                height=1920,
                fps=30.0,
                keyframes=[VideoKeyframe(timestamp_ms=0, image_url="data:image/jpeg;base64,eA==")],
            )
            for video in videos
        ]


class RepairingFakeLLM:
    def __init__(self) -> None:
        self.repair_count = 0
        self.seen_parent_recipe = None
        self.seen_revision_action = None

    def plan_recipe(self, **kwargs):
        self.seen_parent_recipe = kwargs["parent_recipe"]
        self.seen_revision_action = kwargs["revision_action"]
        return EditingPlanDecision(
            outcome="RECIPE",
            recipe=_recipe(invalid_timeline=kwargs["revision_action"] is None),
            publishing=_publishing(),
            missing_scene_roles=[],
            available_options=[],
            rationale="테스트 레시피",
        )

    def repair_recipe(self, **kwargs):
        self.repair_count += 1
        assert kwargs["validation_errors"]
        return EditingPlanDecision(
            outcome="RECIPE",
            recipe=_recipe(),
            publishing=_publishing(),
            missing_scene_roles=[],
            available_options=[],
            rationale="검증 오류 수정",
        )


class SourceGapFakeLLM:
    def plan_recipe(self, **kwargs):
        return EditingPlanDecision(
            outcome="SOURCE_GAP",
            recipe=None,
            publishing=None,
            missing_scene_roles=["RESULT"],
            available_options=["USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"],
            rationale="완성 장면 근거가 없음",
        )

    def repair_recipe(self, **kwargs):
        raise AssertionError("SOURCE_GAP must not enter repair")


class FakeRenderer:
    def __init__(self) -> None:
        self.calls = []

    def render(self, **kwargs):
        self.calls.append(kwargs)
        return EditingRenderResult(
            output_video_url="https://cdn.example/final.mp4",
            resolution="1080x1920",
            duration_sec=4.0,
            cover_image_url="https://cdn.example/cover.jpg",
        )


def _seed_video_editing_db(db) -> None:
    db.add(
        VideoEditingDBRecord(
            template_id="video_editing_db_014",
            version=3,
            status="ACTIVE",
            name="메뉴 공개",
            recommendation_title="한눈에 보는 신메뉴",
            recommendation_concept="과정과 완성 컷을 빠르게 보여줍니다.",
            recommendation_metadata={},
            shooting_guide={"scenes": [{"role": "HOOK"}, {"role": "RESULT"}]},
            editing_rules={
                "min_cut_duration_ms": 300,
                "max_duration_sec": 30,
                "allowed_effect_ids": ["PUNCH_ZOOM"],
                "allowed_transition_ids": ["CUT", "HARD_CUT"],
            },
            trend_ids=[],
        )
    )
    db.commit()


def test_editing_pipeline_repairs_validates_renders_and_revises(monkeypatch):
    llm = RepairingFakeLLM()
    renderer = FakeRenderer()
    service = EditingAgentService(
        llm=llm,
        video_context_builder=FakeVideoContextBuilder(),
        renderer=renderer,
    )
    stages: list[str] = []
    persist_stage = service._set_stage

    def record_stage(db, run, stage, progress):
        stages.append(stage.value)
        persist_stage(db, run, stage, progress)

    monkeypatch.setattr(service, "_set_stage", record_stage)
    with SessionLocal() as db:
        _seed_video_editing_db(db)
        run = service.create_run(db, _request())
        completed = service.execute(db, run.id)

        assert completed.status == EditingRunStatus.COMPLETED.value
        assert completed.stage == "COMPLETED"
        assert completed.progress == 100
        assert completed.recipe["timeline"][0]["timeline_start_ms"] == 0
        assert completed.render_result["output_video_url"].endswith("final.mp4")
        assert completed.publishing_result["post_note"].startswith("음원은")
        assert llm.repair_count == 1
        assert stages == [
            "PREPARING_VIDEO_CONTEXT",
            "PLANNING_RECIPE",
            "VALIDATING_RECIPE",
            "PLANNING_RECIPE",
            "VALIDATING_RECIPE",
            "RENDERING",
        ]
        assert len(renderer.calls) == 1
        assert "image_url" not in completed.video_context[0]["keyframes"][0]

        original_recipe = completed.recipe
        refreshed_videos = [
            video.model_copy(
                update={"footage_url": f"https://cdn.example/refreshed/{video.video_id}.mp4"}
            )
            for video in _request().videos
        ]
        revision = service.create_revision(
            db,
            completed.id,
            EditingRevisionRequest(
                revision_action="첫 장면을 더 짧게 하고 자막을 크게 해줘",
                videos=refreshed_videos,
            ),
        )
        assert [
            video["footage_url"] for video in revision.request_snapshot["videos"]
        ] == [video.footage_url for video in refreshed_videos]
        revised = service.execute(db, revision.id)
        assert revised.status == EditingRunStatus.COMPLETED.value
        assert revised.parent_run_id == completed.id
        assert llm.seen_parent_recipe == original_recipe
        assert llm.seen_revision_action.startswith("첫 장면")
        assert db.get(EditingRun, completed.id).recipe == original_recipe


def test_editing_pipeline_returns_source_gap_without_rendering():
    renderer = FakeRenderer()
    service = EditingAgentService(
        llm=SourceGapFakeLLM(),
        video_context_builder=FakeVideoContextBuilder(),
        renderer=renderer,
    )
    with SessionLocal() as db:
        _seed_video_editing_db(db)
        run = service.create_run(db, _request())
        result = service.execute(db, run.id)
        payload = service.result(result)

        assert result.status == EditingRunStatus.SOURCE_GAP.value
        assert payload.missing_scene_roles == ["RESULT"]
        assert set(payload.available_options) == {
            "USE_REDUCED_STRUCTURE",
            "ADD_MORE_VIDEO",
        }
        assert payload.recipe is None
        assert renderer.calls == []

        refreshed = [
            video.model_copy(update={"footage_url": f"https://cdn.example/new/{video.video_id}"})
            for video in _request().videos
        ]
        refreshed.append(
            refreshed[-1].model_copy(
                update={
                    "video_id": "take_503",
                    "footage_url": "https://cdn.example/new/take_503",
                    "shooting_scene_order": 3,
                }
            )
        )
        revision = service.create_revision(
            db,
            result.id,
            EditingRevisionRequest(
                revision_action="추가 촬영한 영상도 사용해줘",
                videos=refreshed,
            ),
        )
        assert len(revision.request_snapshot["videos"]) == 3


@dataclass
class _QueuedTask:
    id: str = "task-editing-1"


def test_editing_api_contract(client, auth_headers, monkeypatch):
    from app.api.v1 import editing_runs as editing_api

    monkeypatch.setattr(
        editing_api,
        "validate_editing_runtime",
        lambda: {"openai": True, "renderer": True, "ffprobe": True, "ffmpeg": True},
    )
    monkeypatch.setattr(editing_api, "enqueue_editing_pipeline", lambda run_id: _QueuedTask())
    with SessionLocal() as db:
        _seed_video_editing_db(db)

    response = client.post(
        "/api/v1/editing-runs",
        headers=auth_headers,
        json=_request().model_dump(mode="json"),
    )
    assert response.status_code == 202
    created = response.json()
    assert created["run_id"].startswith("edit_")
    assert created["status"] == "QUEUED"
    assert created["task_id"] == "task-editing-1"

    status_response = client.get(
        f"/api/v1/editing-runs/{created['run_id']}", headers=auth_headers
    )
    assert status_response.status_code == 200
    assert status_response.json()["stage"] == "QUEUED"

    result_response = client.get(
        f"/api/v1/editing-runs/{created['run_id']}/result", headers=auth_headers
    )
    assert result_response.status_code == 409


def test_editing_api_marks_run_failed_when_enqueue_fails(client, auth_headers, monkeypatch):
    from app.api.v1 import editing_runs as editing_api

    monkeypatch.setattr(
        editing_api,
        "validate_editing_runtime",
        lambda: {"openai": True, "renderer": True, "ffprobe": True, "ffmpeg": True},
    )

    def fail_enqueue(_: str):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(editing_api, "enqueue_editing_pipeline", fail_enqueue)
    with SessionLocal() as db:
        _seed_video_editing_db(db)

    response = client.post(
        "/api/v1/editing-runs",
        headers=auth_headers,
        json=_request().model_dump(mode="json"),
    )
    assert response.status_code == 503
    run_id = response.json()["detail"]["run_id"]
    with SessionLocal() as db:
        failed = db.get(EditingRun, run_id)
        assert failed is not None
        assert failed.status == "FAILED"
        assert failed.stage == "FAILED"
        assert failed.celery_task_id is None


def test_editing_agent_is_registered(client, auth_headers):
    response = client.get("/api/v1/agents/editing", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["trigger_endpoint"] == "/api/v1/editing-runs"
