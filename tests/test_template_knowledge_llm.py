from __future__ import annotations

import json

from app.template_knowledge import llm
from app.template_knowledge.llm import OpenAITemplateCandidateGenerator
from app.schemas.template_knowledge import TradeAreaEvidence
from tests.template_payloads import trade_area_payload


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
