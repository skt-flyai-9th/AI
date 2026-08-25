from __future__ import annotations

from sqlalchemy import select

from app.db.session import SessionLocal
from app.main import app
from app.models.challenge import Challenge
from app.models.video_editing_db_record import VideoEditingDBRecord
from app.models.template_update_candidate import TemplateUpdateCandidate
from app.models.template_video_analysis import TemplateVideoAnalysis
from app.models.template_knowledge_run import TemplateKnowledgeRun
from app.models.template_source import TemplateSourceBundle, TemplateSourceRecord
from app.models.trade_area_db_record import TradeAreaDBRecord
from app.schemas.template_knowledge import (
    CandidateDecision,
    EditingCandidateCreate,
    VideoEditingDBContent,
    EditingVideoInsight,
    TemplateCandidateStatus,
    TemplateType,
    TradeAreaAnalysisResult,
    TradeAreaAnalyzeRequest,
    TradeAreaCandidateCreate,
    TradeAreaEvidence,
    TradeAreaDBContent,
)
from app.template_knowledge.seeds import seed_template_library
from app.template_knowledge.service import (
    TemplateKnowledgeService,
    get_template_knowledge_service,
)
from app.template_knowledge.llm import _make_strict_schema
from app.template_knowledge.source_library import TemplateSourceService
from tests.template_payloads import trade_area_payload, video_editing_db_payload


def _evidence() -> TradeAreaEvidence:
    return TradeAreaEvidence.model_validate(
        {
            "industry_category": "카페",
            "region_scope": {"district": "관악구"},
            "area_type": "office",
            "signals": {
                "population_by_hour": {"12": 1800, "18": 900},
                "age_distribution": {"20대": 0.38, "30대": 0.34},
                "visits_by_hour": {"12": 1400, "18": 700},
                "age_distribution_sample_size": 800,
            },
            "sources": [
                {
                    "source_id": "district-signal-202608",
                    "source_type": "AGGREGATE_TRADE_AREA_DATA",
                    "observed_at": "2026-08-20T00:00:00Z",
                    "source_url": "https://data.example/trade-area/office",
                    "note": "집계 자료",
                }
            ],
        }
    )


class FakeGenerator:
    model_name = "fake-template-model"

    def generate_trade_area(self, **kwargs) -> TradeAreaDBContent:
        payload = trade_area_payload()
        payload["description"] = "새 집계 근거를 반영한 오피스 상권 분석 템플릿입니다."
        return TradeAreaDBContent.model_validate(payload)

    def generate_editing(self, **kwargs) -> VideoEditingDBContent:
        payload = video_editing_db_payload()
        payload["recommendation_concept"] = "검증된 트렌드 훅을 반영한 메뉴 결과 중심 구성입니다."
        payload["trend_ids"] = [item.trend_id for item in kwargs["insights"]]
        return VideoEditingDBContent.model_validate(payload)

    def analyze_trade_area(self, **kwargs) -> TradeAreaAnalysisResult:
        return TradeAreaAnalysisResult(
            characteristics=["평일 점심 집중형", "직장인 생활 유동"],
            target_age_ranges=["20대", "30대"],
            target_time_ranges=["11:00-14:00"],
            visit_purposes=["점심", "테이크아웃"],
            opportunity_signals=["점심 방문량 우세"],
            cautions=["주말 자료 부족"],
            evidence_source_ids=["district-signal-202608"],
            confidence=0.82,
        )


class FakeVideoAnalyzer:
    model_name = "fake-gemini-video"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, *, trend_id, youtube_url, trend_context) -> EditingVideoInsight:
        self.calls += 1
        return EditingVideoInsight(
            trend_id=trend_id,
            youtube_url=youtube_url,
            summary="완성 메뉴를 첫 2초에 보여주고 제조 장면을 빠르게 연결합니다.",
            hook_patterns=["0-2초 결과 선공개"],
            shot_sequence=["RESULT", "PROCESS", "DETAIL", "CTA"],
            pacing={"median_cut_sec": 1.4, "tempo": "FAST", "opening_hook_sec": 2.0},
            caption_patterns=["짧은 핵심 자막"],
            camera_patterns=["클로즈업", "고정 구도"],
            transition_patterns=["HARD_CUT"],
            audio_role="PLATFORM_MUSIC",
            reusable_editing_rules=["첫 결과 컷은 2초 이내"],
            evidence_notes=["00:01 완성 메뉴 클로즈업"],
            confidence=0.9,
        )


def _service() -> tuple[TemplateKnowledgeService, FakeVideoAnalyzer]:
    video = FakeVideoAnalyzer()
    return TemplateKnowledgeService(generator=FakeGenerator(), video_analyzer=video), video


def test_bootstrap_imports_provided_sources_and_activates_approved_bundles():
    service, _ = _service()
    with SessionLocal() as db:
        result = seed_template_library(db, service=service)
        assert len(result["created"]) == 5
        assert (
            db.scalar(
                select(TradeAreaDBRecord).where(TradeAreaDBRecord.status == "ACTIVE")
            )
            is None
        )
        assert len(list(db.scalars(select(VideoEditingDBRecord)))) == 3
        imported_editing = db.scalar(
            select(VideoEditingDBRecord).where(
                VideoEditingDBRecord.template_id == "gt_jujutsu_transition"
            )
        )
        assert imported_editing is not None
        first_task = imported_editing.shooting_guide["tasks"][0]
        assert first_task["display_order"] == 1
        assert first_task["scene_index"] == 0
        assert first_task["task_title"]
        assert first_task["guide"]["instructions"]
        assert "task_type" not in first_task
        assert "guide_type" not in first_task["guide"]
        assert len(list(db.scalars(select(TemplateSourceBundle)))) == 2
        assert db.scalar(select(TemplateSourceRecord)) is not None
        assert result["trade_area"]["status"] == "ACTIVE"
        assert result["trade_area"]["service_eligible"] is True
        assert not db.scalar(
            select(TemplateUpdateCandidate).where(
                TemplateUpdateCandidate.status == TemplateCandidateStatus.INVALID.value
            )
        )

        second = seed_template_library(db, service=service)
        assert second["created"] == []
        assert len(second["skipped"]) == 5


def test_candidate_lifecycle_creates_new_version_and_archives_base():
    service, _ = _service()
    with SessionLocal() as db:
        service.create_candidate_from_payload(
            db,
            template_type=TemplateType.TRADE_AREA,
            template_id="trade_area_office",
            payload=trade_area_payload(),
            source_evidence={"bootstrap": True},
            generation_model="seed",
            requires_human_approval=False,
        )
        candidate = service.create_trade_area_candidate(
            db,
            TradeAreaCandidateCreate(
                template_id="trade_area_office",
                evidence=_evidence(),
                requires_human_approval=True,
            ),
        )
        assert candidate.base_version == 1
        assert candidate.proposed_version == 2
        assert candidate.status == "VALIDATED"
        assert any(item["path"] == "$.description" for item in candidate.diff)

        applied = service.approve_candidate(
            db,
            candidate.id,
            CandidateDecision(actor="template-reviewer", note="근거와 diff 확인"),
        )
        assert applied.status == "APPLIED"
        assert db.get(TradeAreaDBRecord, ("trade_area_office", 1)).status == "ARCHIVED"
        assert db.get(TradeAreaDBRecord, ("trade_area_office", 2)).status == "ACTIVE"
        assert (
            db.get(TradeAreaDBRecord, ("trade_area_office", 1)).description
            != db.get(TradeAreaDBRecord, ("trade_area_office", 2)).description
        )


def test_editing_candidate_rejects_tts_before_activation():
    service, _ = _service()
    invalid = video_editing_db_payload()
    invalid["recommendation_metadata"]["requires_tts"] = True
    with SessionLocal() as db:
        candidate = service.create_candidate_from_payload(
            db,
            template_type=TemplateType.VIDEO_EDITING,
            template_id="invalid_tts",
            payload=invalid,
            source_evidence={"test": True},
            generation_model="test",
            requires_human_approval=True,
        )
        assert candidate.status == "INVALID"
        assert "TTS_FORBIDDEN" in {item["code"] for item in candidate.validation_errors}
        assert db.get(VideoEditingDBRecord, ("invalid_tts", 1)) is None


def test_trend_video_analysis_generates_editing_candidate_and_uses_cache():
    service, video = _service()
    with SessionLocal() as db:
        db.add(
            Challenge(
                id="trend_001",
                automatic_name="메뉴 결과 먼저 공개",
                category="food",
                automatic_rank=1,
                automatic_score=91.0,
                lifecycle="RISING",
                kr_affinity=0.9,
                confidence=0.88,
                automatic_representative_youtube_url="https://www.youtube.com/watch?v=test001",
                representative_video_metadata={"title": "참여 영상"},
                raw_details={},
            )
        )
        db.commit()
        first = service.create_editing_candidate(
            db,
            EditingCandidateCreate(
                template_id="edit_trend_reveal",
                trend_ids=["trend_001"],
            ),
        )
        assert first.status == "VALIDATED"
        assert first.proposed_payload["trend_ids"] == ["trend_001"]
        assert len(first.source_evidence["video_analysis_ids"]) == 1
        assert video.calls == 1

        analysis = db.scalar(select(TemplateVideoAnalysis))
        assert analysis.status == "COMPLETED"
        service.analyze_reference_video(
            db,
            trend_id="trend_001",
            youtube_url="https://www.youtube.com/watch?v=test001",
            trend_context={"trend_id": "trend_001"},
        )
        assert video.calls == 1


def test_trade_area_analysis_uses_active_template_and_persists_result():
    service, _ = _service()
    with SessionLocal() as db:
        seed_template_library(db, service=service)
        service.create_candidate_from_payload(
            db,
            template_type=TemplateType.TRADE_AREA,
            template_id="trade_area_office",
            payload=trade_area_payload(),
            source_evidence={"test": True},
            generation_model="test",
            requires_human_approval=False,
        )
        analysis = service.analyze_trade_area(
            db,
            TradeAreaAnalyzeRequest(evidence=_evidence()),
        )
        assert analysis.template_id == "trade_area_office"
        assert analysis.result.target_age_ranges == ["20대", "30대"]
        assert analysis.result.evidence_source_ids == ["district-signal-202608"]


def test_template_knowledge_api_bootstrap_and_async_analysis(client, auth_headers, monkeypatch):
    from app.api.v1 import database_knowledge as database_api

    class FakeTask:
        id = "task-template-1"

    service, _ = _service()
    app.dependency_overrides[get_template_knowledge_service] = lambda: service
    monkeypatch.setattr(database_api, "_require_runtime", lambda operation: None)
    monkeypatch.setattr(database_api, "enqueue_database_knowledge", lambda run_id: FakeTask())
    try:
        created = client.post("/api/v1/database-knowledge/bootstrap", headers=auth_headers)
        assert created.status_code == 201
        versions = client.get(
            "/api/v1/database-knowledge/databases?status=ACTIVE",
            headers=auth_headers,
        )
        assert versions.status_code == 200
        assert len(versions.json()) == 3
        sources = client.get("/api/v1/database-knowledge/sources", headers=auth_headers)
        assert sources.status_code == 200
        assert len(sources.json()) == 2

        with SessionLocal() as db:
            service.create_candidate_from_payload(
                db,
                template_type=TemplateType.TRADE_AREA,
                template_id="trade_area_office",
                payload=trade_area_payload(),
                source_evidence={"test": True},
                generation_model="test",
                requires_human_approval=False,
            )

        analyzed = client.post(
            "/api/v1/database-knowledge/trade-area-db/analyze",
            headers=auth_headers,
            json={"evidence": _evidence().model_dump(mode="json")},
        )
        assert analyzed.status_code == 202
        run_id = analyzed.json()["run_id"]
        assert analyzed.json()["status"] == "QUEUED"
        with SessionLocal() as db:
            completed = service.execute_run(db, run_id)
            assert completed.status == "COMPLETED"
        result = client.get(
            f"/api/v1/database-knowledge/runs/{run_id}/result",
            headers=auth_headers,
        )
        assert result.status_code == 200
        assert result.json()["result"]["analysis"]["template_id"] == "trade_area_office"
    finally:
        app.dependency_overrides.pop(get_template_knowledge_service, None)


def test_trade_area_source_context_uses_provisionally_approved_workbook():
    service, _ = _service()
    with SessionLocal() as db:
        seed_template_library(db, service=service)
        source = TemplateSourceService()
        safe = source.resolve_trade_area_context(
            db,
            region_id="REG-SEOCHON",
            category_id="CAT-CAF",
        )
        assert safe.region["name"] == "서촌"
        assert safe.category["name"] == "카페"
        assert safe.region_category_fit["fit_score(0~1)"] is not None
        assert safe.draft_data_included is False

        review = source.resolve_trade_area_context(
            db,
            region_id="REG-SEOCHON",
            category_id="CAT-CAF",
            include_draft=True,
        )
        assert review.region["name"] == "서촌"
        assert not any(key.startswith("age_") for key in review.region)
        assert review.category["name"] == "카페"
        assert review.region_category_fit["fit_score(0~1)"] is not None
        assert review.draft_data_included is True


def test_template_knowledge_api_requires_internal_key(client):
    response = client.get("/api/v1/database-knowledge/databases")
    assert response.status_code == 401


def test_template_knowledge_api_marks_run_failed_when_enqueue_fails(
    client, auth_headers, monkeypatch
):
    from app.api.v1 import database_knowledge as database_api

    service, _ = _service()
    app.dependency_overrides[get_template_knowledge_service] = lambda: service
    monkeypatch.setattr(database_api, "_require_runtime", lambda operation: None)

    def fail_enqueue(run_id):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(database_api, "enqueue_database_knowledge", fail_enqueue)
    try:
        response = client.post(
            "/api/v1/database-knowledge/trade-area-db/candidates",
            headers=auth_headers,
            json={
                "template_id": "trade_area_office",
                "evidence": _evidence().model_dump(mode="json"),
                "requires_human_approval": True,
            },
        )
        assert response.status_code == 503
        run_id = response.json()["detail"]["run_id"]
        with SessionLocal() as db:
            failed = db.get(TemplateKnowledgeRun, run_id)
            assert failed.status == "FAILED"
            assert failed.error["code"] == "DATABASE_RUN_ENQUEUE_FAILED"
    finally:
        app.dependency_overrides.pop(get_template_knowledge_service, None)


def test_llm_output_schemas_are_strict_and_have_no_open_objects():
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            if value.get("type") == "object" or "properties" in value:
                yield value
            for item in value.values():
                yield from walk(item)

    for model in (TradeAreaDBContent, VideoEditingDBContent, EditingVideoInsight):
        schema = _make_strict_schema(model.model_json_schema())
        objects = list(walk(schema))
        assert objects
        assert all(item.get("additionalProperties") is False for item in objects)
        assert all("properties" in item for item in objects)
