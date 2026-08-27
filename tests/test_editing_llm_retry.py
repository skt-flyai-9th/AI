from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

import pytest

from app.agents.editing.llm import EditingLLMError, OpenAIEditingLLM
from app.agents.editing import structured_output
from app.agents.editing.types import EditingPlanDecision


@dataclass
class _FakeResponse:
    status_code: int
    payload: dict[str, Any]
    headers: dict[str, str] | None = None

    def json(self) -> dict[str, Any]:
        return self.payload


def _planner() -> OpenAIEditingLLM:
    planner = OpenAIEditingLLM()
    planner.api_key = "test-key"
    planner.model = "test-model"
    planner.max_output_tokens = 5000
    planner.max_request_attempts = 3
    planner.rate_limit_retry_base_seconds = 20.0
    return planner


def _source_gap_payload() -> dict[str, Any]:
    return EditingPlanDecision(
        outcome="SOURCE_GAP",
        recipe=None,
        publishing=None,
        missing_scene_roles=["RESULT"],
        available_options=["USE_REDUCED_STRUCTURE", "ADD_MORE_VIDEO"],
        rationale="result footage is missing",
    ).model_dump(mode="json")


def _recipe_payload_without_publishing_title() -> dict[str, Any]:
    return {
        "outcome": "RECIPE",
        "recipe": {
            "recipe_version": 1,
            "editing_template_id": "template_1",
            "editing_template_version": 1,
            "source_type": "VIDEO_ONLY",
            "timeline": [
                {
                    "clip_order": 1,
                    "video_id": "video_1",
                    "source_start_ms": 0,
                    "source_end_ms": 1000,
                    "timeline_start_ms": 0,
                }
            ],
            "cta": {"text": "지금 만나보세요"},
        },
        "publishing": {
            "caption": "칙촉의 비주얼을 매장에서 만나보세요.",
            "hashtags": ["#칙촉", "#매장소개", "#맛집", "#숏폼", "#릴스"],
            "track": {
                "mode": "SUGGESTED",
                "search_keyword": "주술회전 감성 손동작 소환",
                "start_sec": None,
                "end_sec": None,
            },
            "post_note": "주술회전 감성 손동작 소환을 검색해 추가해주세요.",
        },
        "missing_scene_roles": [],
        "available_options": [],
        "rationale": "메뉴 홍보 편집",
    }


def _output(payload: dict[str, Any], *, status: str = "completed") -> dict[str, Any]:
    return {
        "status": status,
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(payload, ensure_ascii=False),
                    }
                ]
            }
        ],
    }


def _install_responses(monkeypatch, responses: list[_FakeResponse]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def fake_post(**kwargs):
        requests.append(copy.deepcopy(kwargs["request_payload"]))
        return responses.pop(0)

    monkeypatch.setattr(structured_output, "_post_responses_api", fake_post)
    monkeypatch.setattr(
        structured_output,
        "_wait_before_retry",
        lambda _attempt, **_kwargs: None,
    )
    return requests


def test_invalid_structured_output_retries_only_the_failed_model_call(monkeypatch):
    requests = _install_responses(
        monkeypatch,
        [
            _FakeResponse(200, _output({})),
            _FakeResponse(200, _output(_source_gap_payload())),
        ],
    )

    result = _planner()._request_model(
        schema_model=EditingPlanDecision,
        instructions="Plan an edit.",
        user_payload={"project": "test"},
        schema_name="editing_plan",
    )

    assert result.outcome == "SOURCE_GAP"
    assert len(requests) == 2
    assert [item["max_output_tokens"] for item in requests] == [5000, 10000]
    assert "previous response could not be validated" in requests[1]["instructions"]


def test_missing_publishing_title_is_recovered_from_valid_caption(monkeypatch):
    requests = _install_responses(
        monkeypatch,
        [_FakeResponse(200, _output(_recipe_payload_without_publishing_title()))],
    )

    result = _planner()._request_model(
        schema_model=EditingPlanDecision,
        instructions="Plan a menu promotion edit.",
        user_payload={},
        schema_name="editing_plan",
    )

    assert result.publishing is not None
    assert result.publishing.title == "칙촉의 비주얼을 매장에서 만나보세요."
    assert len(requests) == 1


def test_incomplete_output_increases_token_limit_before_retry(monkeypatch):
    requests = _install_responses(
        monkeypatch,
        [
            _FakeResponse(
                200,
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [],
                },
            ),
            _FakeResponse(200, _output(_source_gap_payload())),
        ],
    )

    result = _planner()._request_model(
        schema_model=EditingPlanDecision,
        instructions="Plan an edit.",
        user_payload={},
        schema_name="editing_plan",
    )

    assert result.outcome == "SOURCE_GAP"
    assert requests[1]["max_output_tokens"] == 10000
    assert "incomplete_max_output_tokens" in requests[1]["instructions"]


def test_invalid_output_fails_only_after_bounded_retries(monkeypatch):
    requests = _install_responses(
        monkeypatch,
        [_FakeResponse(200, _output({})) for _ in range(3)],
    )

    with pytest.raises(EditingLLMError) as captured:
        _planner()._request_model(
            schema_model=EditingPlanDecision,
            instructions="Plan an edit.",
            user_payload={},
            schema_name="editing_plan",
        )

    assert len(requests) == 3
    assert captured.value.retryable is False
    assert "schema=editing_plan" in str(captured.value)
    assert "schema_validation" in str(captured.value)
    assert "attempt=3/3" in str(captured.value)


def test_non_retryable_http_error_still_fails_immediately(monkeypatch):
    requests = _install_responses(monkeypatch, [_FakeResponse(400, {})])

    with pytest.raises(EditingLLMError) as captured:
        _planner()._request_model(
            schema_model=EditingPlanDecision,
            instructions="Plan an edit.",
            user_payload={},
            schema_name="editing_plan",
        )

    assert len(requests) == 1
    assert captured.value.retryable is False
    assert "reason=http_400" in str(captured.value)


def test_rate_limit_response_is_retried(monkeypatch):
    requests = _install_responses(
        monkeypatch,
        [
            _FakeResponse(429, {}),
            _FakeResponse(200, _output(_source_gap_payload())),
        ],
    )

    result = _planner()._request_model(
        schema_model=EditingPlanDecision,
        instructions="Plan an edit.",
        user_payload={},
        schema_name="editing_plan",
    )

    assert result.outcome == "SOURCE_GAP"
    assert len(requests) == 2
    assert requests[1]["max_output_tokens"] == 5000


def test_rate_limit_uses_long_backoff_and_honors_retry_after(monkeypatch):
    waits: list[float] = []
    responses = [
        _FakeResponse(429, {}, headers={"retry-after": "35"}),
        _FakeResponse(200, _output(_source_gap_payload())),
    ]
    requests: list[dict[str, Any]] = []

    def fake_post(**kwargs):
        requests.append(copy.deepcopy(kwargs["request_payload"]))
        return responses.pop(0)

    monkeypatch.setattr(structured_output, "_post_responses_api", fake_post)
    monkeypatch.setattr(
        structured_output,
        "_wait_before_retry",
        lambda _attempt, *, minimum_seconds=0.0: waits.append(minimum_seconds),
    )

    result = _planner()._request_model(
        schema_model=EditingPlanDecision,
        instructions="Plan an edit.",
        user_payload={},
        schema_name="editing_plan",
    )

    assert result.outcome == "SOURCE_GAP"
    assert len(requests) == 2
    assert waits == [35.0]


@pytest.mark.parametrize("code", ["credit_balance_exhausted", "insufficient_quota"])
def test_exhausted_quota_fails_immediately_without_retry(monkeypatch, code):
    requests = _install_responses(
        monkeypatch,
        [
            _FakeResponse(
                429,
                {"error": {"type": "insufficient_quota", "code": code}},
            )
        ],
    )

    with pytest.raises(EditingLLMError) as captured:
        _planner()._request_model(
            schema_model=EditingPlanDecision,
            instructions="Plan an edit.",
            user_payload={},
            schema_name="editing_plan",
        )

    assert len(requests) == 1
    assert captured.value.retryable is False
    assert code in str(captured.value)
