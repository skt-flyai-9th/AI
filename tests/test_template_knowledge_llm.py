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
        estimated_shooting_time_bucket="within_10m",
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
            "estimated_shooting_time_bucket": "within_10m",
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


def test_gemini_retries_until_human_reviewed_cut_count_is_reproduced(monkeypatch):
    prompts = []

    def fake_call(**kwargs):
        prompts.append(json.loads(kwargs["user_prompt"]))
        count = 6 if len(prompts) == 1 else 7
        return {
            "trend_id": "ignored",
            "youtube_url": "https://www.youtube.com/watch?v=example",
            "summary": "사람의 자세 점프를 기준으로 컷을 분리한다.",
            "hook_patterns": ["high-angle hook"],
            "shot_sequence": [f"CUT_{index}" for index in range(1, count + 1)],
            "segments": [
                {
                    "sequence": index,
                    "start_sec": float(index - 1),
                    "end_sec": float(index),
                    "scene_role": f"CUT_{index}",
                    "description": "인물의 자세가 불연속적으로 바뀐다.",
                    "shot_type": "HIGH_ANGLE",
                    "transition_out": "HARD_CUT" if index < count else None,
                    "evidence": f"{index - 1}.0초 자세 점프",
                }
                for index in range(1, count + 1)
            ],
            "estimated_shooting_time_bucket": "within_10m",
            "pacing": {"tempo": "FAST", "median_cut_sec": 1.0, "opening_hook_sec": 1.0},
            "caption_patterns": [],
            "camera_patterns": [],
            "transition_patterns": ["HARD_CUT"],
            "audio_role": "PLATFORM_MUSIC",
            "reusable_editing_rules": ["자세 점프마다 컷 분리"],
            "evidence_notes": ["육안 검수 7컷"],
            "confidence": 0.95,
        }

    monkeypatch.setattr(llm, "call_gemini_structured", fake_call)
    analyzer = GeminiYouTubeVideoAnalyzer()
    analyzer.api_key = "test-gemini-key"
    analyzer._resolved_model_name = "gemini-test"
    result = analyzer.analyze(
        trend_id="otsukare_summer_challenge",
        youtube_url="https://www.youtube.com/watch?v=example",
        trend_context={
            "raw_details": {
                "reference_cut_review": {
                    "status": "HUMAN_REVIEWED",
                    "expected_cut_count": 7,
                    "boundary_basis": ["사람의 자세가 뚝 바뀌면 새 컷"],
                }
            }
        },
    )

    assert len(prompts) == 2
    assert len(result.segments) == 7
    assert prompts[0]["human_reviewed_reference_cut_review"]["expected_cut_count"] == 7
    assert "returned 6 cuts" in prompts[1]["correction"]
    assert len(prompts[1]["previous_mismatched_cut_analysis"]["segments"]) == 6
