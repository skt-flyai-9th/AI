from __future__ import annotations

import json

from app.template_knowledge import llm
from app.template_knowledge.llm import (
    GeminiYouTubeVideoAnalyzer,
    OpenAITemplateCandidateGenerator,
)
from app.schemas.template_knowledge import (
    EditingVideoInsight,
    MAX_SHOOTING_GUIDE_CUTS,
    TradeAreaEvidence,
    VideoEditingDBContent,
)
from tests.template_payloads import trade_area_payload, video_editing_db_payload


def test_openai_template_generator_uses_strict_structured_output(monkeypatch):
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "output": [
                    {
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    trade_area_payload(),
                                    ensure_ascii=False,
                                ),
                            }
                        ]
                    }
                ]
            }

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["request"] = kwargs
            return FakeResponse()

    monkeypatch.setattr(llm.httpx, "Client", FakeClient)
    generator = OpenAITemplateCandidateGenerator()
    generator.api_key = "test-openai-key"
    generator._model_name = "test-template-model"
    result = generator.generate_trade_area(
        template_id="trade_area_office",
        base_payload=None,
        evidence=TradeAreaEvidence.model_validate(
            {
                "industry_category": "카페",
                "region_scope": {"district": "관악구"},
                "area_type": "office",
                "signals": {"visits_by_hour": {"12": 100}},
                "sources": [
                    {
                        "source_id": "source-1",
                        "source_type": "AGGREGATE",
                    }
                ],
            }
        ),
    )
    request = captured["request"]["json"]
    schema = request["text"]["format"]["schema"]
    assert captured["url"].endswith("/responses")
    assert request["model"] == "test-template-model"
    assert request["store"] is False
    assert request["text"]["format"]["strict"] is True
    assert schema["additionalProperties"] is False
    assert result.name == "오피스 상권 분석 테스트"


def test_editing_generator_preserves_physical_edit_cut_boundaries(monkeypatch):
    captured = {}
    generator = OpenAITemplateCandidateGenerator()

    def capture_request(**kwargs):
        captured.update(kwargs)
        return VideoEditingDBContent.model_validate(video_editing_db_payload())

    monkeypatch.setattr(generator, "_request", capture_request)
    insight = EditingVideoInsight(
        trend_id="trend-1",
        youtube_url="https://www.youtube.com/watch?v=example",
        summary="reference summary",
        hook_patterns=["hook"],
        shot_sequence=["HOOK", "RESULT"],
        segments=[
            {
                "sequence": 1,
                "start_sec": 0.0,
                "end_sec": 1.0,
                "scene_role": "HOOK",
                "description": "결과를 먼저 보여준다.",
                "shot_type": "CLOSE_UP",
                "transition_out": "HARD_CUT",
                "evidence": "0.0-1.0초 결과 클로즈업",
            },
            {
                "sequence": 2,
                "start_sec": 1.0,
                "end_sec": 2.0,
                "scene_role": "RESULT",
                "description": "완성 결과를 유지한다.",
                "shot_type": "MEDIUM",
                "transition_out": None,
                "evidence": "1.0-2.0초 완성 결과",
            },
        ],
        pacing={"tempo": "FAST", "median_cut_sec": 1.0, "opening_hook_sec": 1.0},
        caption_patterns=[],
        camera_patterns=[],
        transition_patterns=[],
        audio_role="PLATFORM_MUSIC",
        reusable_editing_rules=["rule"],
        evidence_notes=["evidence"],
        confidence=0.9,
    )

    generator.generate_editing(
        template_id="gt_test",
        base_payload=None,
        trend_context=[],
        insights=[insight],
    )

    rules = captured["payload"]["guide_authoring_rules"]
    assert any(f"at most {MAX_SHOOTING_GUIDE_CUTS}" in rule for rule in rules)
    assert any("natural Korean" in rule for rule in rules)
    assert any("visible edit discontinuity" in rule for rule in rules)
    schema = captured["schema_model"].model_json_schema()
    guide = schema["$defs"]["EditingShootingGuide"]["properties"]
    assert guide["scenes"]["maxItems"] == MAX_SHOOTING_GUIDE_CUTS
    assert guide["tasks"]["maxItems"] == MAX_SHOOTING_GUIDE_CUTS


def test_gemini_analysis_requires_frame_discontinuity_cut_boundaries(monkeypatch):
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return {
            "trend_id": "ignored",
            "youtube_url": "https://www.youtube.com/watch?v=example",
            "summary": "reference summary",
            "hook_patterns": ["hook"],
            "shot_sequence": ["HOOK", "RESULT"],
            "segments": [
                {
                    "sequence": 1,
                    "start_sec": 0.0,
                    "end_sec": 1.0,
                    "scene_role": "HOOK",
                    "description": "결과를 먼저 보여준다.",
                    "shot_type": "CLOSE_UP",
                    "transition_out": "HARD_CUT",
                    "evidence": "0.0-1.0초 결과 클로즈업",
                },
                {
                    "sequence": 2,
                    "start_sec": 1.0,
                    "end_sec": 2.0,
                    "scene_role": "RESULT",
                    "description": "완성 결과를 유지한다.",
                    "shot_type": "MEDIUM",
                    "transition_out": None,
                    "evidence": "1.0-2.0초 완성 결과",
                },
            ],
            "pacing": {
                "tempo": "FAST",
                "median_cut_sec": 1.0,
                "opening_hook_sec": 1.0,
            },
            "caption_patterns": [],
            "camera_patterns": [],
            "transition_patterns": [],
            "audio_role": "PLATFORM_MUSIC",
            "reusable_editing_rules": ["rule"],
            "evidence_notes": ["evidence"],
            "confidence": 0.9,
        }

    monkeypatch.setattr(llm, "call_gemini_structured", fake_call)
    analyzer = GeminiYouTubeVideoAnalyzer()
    analyzer.api_key = "test-gemini-key"
    analyzer._resolved_model_name = "gemini-test"
    analyzer.analyze(
        trend_id="trend-1",
        youtube_url="https://www.youtube.com/watch?v=example",
        trend_context={},
    )

    prompt = json.loads(captured["user_prompt"])
    assert f"no more than {MAX_SHOOTING_GUIDE_CUTS}" in prompt["task"]
    assert "object suddenly appears" in prompt["task"]
    assert "pose or screen position jumps" in prompt["task"]
    assert any("food item popping" in rule for rule in prompt["cut_boundary_rules"])
    assert any("person disappearing" in rule for rule in prompt["cut_boundary_rules"])
    assert (
        captured["schema"]["properties"]["shot_sequence"]["maxItems"]
        == MAX_SHOOTING_GUIDE_CUTS
    )
    assert captured["schema"]["properties"]["segments"]["maxItems"] == MAX_SHOOTING_GUIDE_CUTS
    assert "segments" in prompt["task"]
