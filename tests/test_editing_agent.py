from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.agents.editing.service import EditingAgentService, EditingDomainError
from app.agents.editing.types import EditingPlanDecision, VideoContext, VideoKeyframe
from app.agents.editing.video_context import FFmpegVideoContextBuilder, VideoContextError
from app.core.config import Settings
from app.db.session import SessionLocal
from app.models.editing_run import EditingRun
from app.models.shortform_session import ShortformSession
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.schemas.editing import (
    EditRecipe,
    EditingRenderResult,
    EditingRevisionRequest,
    EditingRunCreateRequest,
    EditingRunStatus,
    PublishingResult,
    RecipeCta,
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
                "editing_template_id": "video_editing_db_014",
                "editing_template_version": 3,
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
            "editing_template_id": "video_editing_db_014",
            "editing_template_version": 3,
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
                        "style_id": "HOOK",
                        "motion_id": "TYPEWRITER",
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
                    "caption": {
                        "text": "한눈에 만나는 특별한 메뉴",
                        "start_ms": 2000,
                        "end_ms": 3500,
                        "position": "MIDDLE",
                        "style_id": "CAPTION_EMPHASIS",
                        "motion_id": "POP",
                        "font_weight": "BOLD",
                        "scale": 1.0,
                    },
                    "effects": [],
                },
            ],
            "cta": {"text": "지금 매장에서 만나보세요"},
        }
    )


def _publishing() -> PublishingResult:
    return PublishingResult(
        title="오늘의 딸기 크림 라떼",
        caption="딸기 크림 라떼를 만나보세요.",
        hashtags=["#딸기라떼", "#카페신메뉴", "#카페추천", "#신메뉴", "#숏폼"],
        track={
            "mode": "SUGGESTED",
            "title": None,
            "artist": None,
            "start_sec": None,
            "end_sec": None,
            "mood": None,
            "search_keyword": "딸기 라떼 릴스",
        },
        post_note="플랫폼 음원 검색에서 ‘딸기 라떼 릴스’을 검색해 직접 추가해주세요.",
    )


def test_publishing_contract_requires_five_hashtags_and_search_keyword():
    payload = _publishing().model_dump(mode="json")
    payload["hashtags"] = payload["hashtags"][:4]
    with pytest.raises(ValueError, match="at least 5 items"):
        PublishingResult.model_validate(payload)

    payload = _publishing().model_dump(mode="json")
    payload["track"]["search_keyword"] = None
    with pytest.raises(ValueError, match="search_keyword"):
        PublishingResult.model_validate(payload)


def test_video_cta_rejects_platform_music_instructions():
    with pytest.raises(ValueError, match="operational music/upload instructions"):
        RecipeCta(text="음악은 플랫폼에서 직접 추가하세요")


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
        self.seen_project = None

    def plan_recipe(self, **kwargs):
        self.seen_parent_recipe = kwargs["parent_recipe"]
        self.seen_revision_action = kwargs["revision_action"]
        self.seen_project = kwargs["project"]
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
    def __init__(self) -> None:
        self.plan_count = 0

    def plan_recipe(self, **kwargs):
        self.plan_count += 1
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


def _seed_video_editing_db(db, *, status: str = "ACTIVE") -> None:
    db.add(
        VideoEditingDBRecord(
            template_id="video_editing_db_014",
            version=3,
            status=status,
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


def _seed_shortform_session(db) -> None:
    db.add(
        ShortformSession(
            id="shortform_123",
            status="WAITING_RECOMMENDATION_ACTION",
            store_id="store_123",
            store_context={
                "store": {
                    "store_id": "store_123",
                    "store_name": "사릴스 카페",
                    "category": "카페",
                    "store_photos": [{"asset_id": "photo_1", "asset_url": "private"}],
                },
                "representative_menus": [
                    {"menu_id": "menu_001", "name": "딸기 크림 라떼", "price": 6500}
                ],
            },
            project_state={
                "promotion_subject": {"type": "MENU", "name": "딸기 크림 라떼"},
                "promotion_objective": "sales",
                "creative_preferences": ["상큼하고 빠른 분위기"],
                "secondary_information": ["매일 아침 직접 만든 딸기청"],
                "facts_from_user": {"taste": "생딸기가 씹히는 상큼한 맛"},
                "brief_confirmed": True,
            },
            conversation=[
                {"role": "user", "content": "수제 딸기청을 꼭 강조해줘"},
                {"role": "assistant", "content": "알겠습니다"},
            ],
            shown_video_editing_db_ids=["video_editing_db_014"],
            current_recommendation={
                "recommendation_id": "rec_123",
                "title": "딸기 포인트 공개",
                "concept": "수제 딸기청을 빠르게 강조",
                "editing_template_id": "video_editing_db_014",
                "editing_template_version": 3,
            },
        )
    )
    db.commit()


def test_archived_pinned_database_version_remains_executable():
    service = EditingAgentService(
        llm=RepairingFakeLLM(),
        video_context_builder=FakeVideoContextBuilder(),
        renderer=FakeRenderer(),
    )
    with SessionLocal() as db:
        _seed_video_editing_db(db, status="ARCHIVED")
        run = service.create_run(db, _request())

    assert run.status == EditingRunStatus.QUEUED.value


def test_editing_run_rejects_more_videos_than_free_tier_limit():
    request_payload = _request().model_dump(mode="json")
    request_payload["videos"] = [
        {
            "video_id": f"take_{index}",
            "footage_url": f"https://cdn.example/take-{index}.mp4",
            "shooting_scene_order": index,
        }
        for index in range(1, 8)
    ]
    request = EditingRunCreateRequest.model_validate(request_payload)
    service = EditingAgentService(
        llm=RepairingFakeLLM(),
        video_context_builder=FakeVideoContextBuilder(),
        renderer=FakeRenderer(),
    )
    service.settings = Settings(editing_max_videos_per_run=6)

    with SessionLocal() as db, pytest.raises(EditingDomainError) as error:
        service.create_run(db, request)

    assert error.value.code == "EDITING_VIDEO_LIMIT_EXCEEDED"
    assert error.value.status_code == 422


def test_video_context_rejects_source_over_cpu_profile_limit(monkeypatch):
    builder = FFmpegVideoContextBuilder()
    builder.max_source_duration_ms = 30_000
    monkeypatch.setattr(
        "app.agents.editing.video_context.download_source_asset",
        lambda _url, target, **_kwargs: target.write_bytes(b"video"),
    )
    monkeypatch.setattr(
        builder,
        "_probe",
        lambda *_: {"duration_ms": 30_001, "width": 1080, "height": 1920, "fps": 30.0},
    )
    monkeypatch.setattr(
        builder,
        "_extract_keyframes",
        lambda *_: pytest.fail("over-limit input must not be decoded"),
    )

    with pytest.raises(VideoContextError, match="30000ms limit"):
        builder._build_one(_request().videos[0])


def test_video_context_normalizes_source_after_initial_frame_extraction_failure(monkeypatch):
    builder = FFmpegVideoContextBuilder()
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(command)
        if len(commands) == 1:
            return subprocess.CompletedProcess(command, 1, stderr=b"non-monotonic timestamp")
        if len(commands) == 2:
            Path(command[-1]).write_bytes(b"normalized-video")
            return subprocess.CompletedProcess(command, 0, stderr=b"")
        pattern = Path(command[-1])
        (pattern.parent / "frame-000001.jpg").write_bytes(b"jpeg")
        return subprocess.CompletedProcess(command, 0, stderr=b"")

    monkeypatch.setattr("app.agents.editing.video_context.subprocess.run", run)

    frames = builder._extract_keyframes("source.mp4", "video-1", [0])

    assert len(frames) == 1
    assert len(commands) == 3
    assert "+genpts" in commands[1]
    assert "libx264" in commands[1]


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
        _seed_shortform_session(db)
        run = service.create_run(db, _request())
        assert run.request_snapshot["_shortform_context"]["session_id"] == "shortform_123"
        assert "store_photos" not in run.request_snapshot["_shortform_context"]["store_context"]["store"]
        completed = service.execute(db, run.id)

        assert completed.status == EditingRunStatus.COMPLETED.value
        assert completed.stage == "COMPLETED"
        assert completed.progress == 100
        assert completed.recipe["timeline"][0]["timeline_start_ms"] == 0
        assert completed.render_result["output_video_url"].endswith("final.mp4")
        assert "딸기 라떼 릴스" in completed.publishing_result["post_note"]
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
        assert llm.seen_project["shortform_context"]["project_state"]["facts_from_user"] == {
            "taste": "생딸기가 씹히는 상큼한 맛"
        }

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
        assert revision.request_snapshot["_shortform_context"] == completed.request_snapshot[
            "_shortform_context"
        ]
        revised = service.execute(db, revision.id)
        assert revised.status == EditingRunStatus.COMPLETED.value
        assert revised.parent_run_id == completed.id
        assert llm.seen_parent_recipe == original_recipe
        assert llm.seen_revision_action.startswith("첫 장면")
        assert db.get(EditingRun, completed.id).recipe == original_recipe


def test_editing_pipeline_renders_ordered_fallback_after_source_gap():
    renderer = FakeRenderer()
    llm = SourceGapFakeLLM()
    service = EditingAgentService(
        llm=llm,
        video_context_builder=FakeVideoContextBuilder(),
        renderer=renderer,
    )
    with SessionLocal() as db:
        _seed_video_editing_db(db)
        _seed_shortform_session(db)
        run = service.create_run(db, _request())
        result = service.execute(db, run.id)
        payload = service.result(result)

        assert result.status == EditingRunStatus.COMPLETED.value
        assert payload.missing_scene_roles == ["RESULT"]
        assert payload.available_options == []
        assert payload.recipe is not None
        assert [clip.video_id for clip in payload.recipe.timeline] == ["take_501", "take_502"]
        assert all(clip.caption is not None for clip in payload.recipe.timeline)
        assert payload.recipe.timeline[0].caption.style_id == "HOOK"
        assert payload.recipe.timeline[0].caption.motion_id == "TYPEWRITER"
        assert payload.recipe.timeline[1].caption.style_id == "CAPTION_EMPHASIS"
        assert payload.recipe.timeline[1].caption.text == "생딸기가 씹히는 상큼한 맛"
        assert "딸기 크림 라떼" in payload.recipe.cta.text
        assert len(renderer.calls) == 1
        assert llm.plan_count == 2
        assert any("SOURCE_ROLE_MATCH_FALLBACK" in item for item in payload.warnings)


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
