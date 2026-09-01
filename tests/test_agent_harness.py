from types import SimpleNamespace

import pandas as pd
import pytest

from app.agents.challenge_ranking.harness import challenge_ranking_harness
from app.agents.harness import (
    AgentHarness,
    AgentHarnessContractError,
    AgentHarnessValidationError,
    HarnessContract,
)


def _harness(events):
    return AgentHarness(
        agent_id="test-agent",
        contracts={
            "run": HarnessContract(
                required_inputs=("request",),
                required_outputs=("result",),
            )
        },
        event_sink=events.append,
    )


def test_harness_validates_and_emits_correlated_lifecycle_events():
    events = []
    harness = _harness(events)

    result = harness.execute(
        operation="run",
        input_value={"request": "value"},
        executor=lambda _: SimpleNamespace(result="ok"),
        correlation_id="run-123",
    )

    assert result.result == "ok"
    assert [event.phase for event in events] == ["STARTED", "SUCCEEDED"]
    assert {event.context.correlation_id for event in events} == {"run-123"}
    assert len({event.context.invocation_id for event in events}) == 1
    assert events[-1].duration_ms is not None


def test_harness_rejects_missing_input_before_executor_runs():
    events = []
    harness = _harness(events)
    executed = False

    def executor(_):
        nonlocal executed
        executed = True
        return {"result": "unreachable"}

    with pytest.raises(AgentHarnessContractError) as captured:
        harness.execute(
            operation="run",
            input_value={},
            executor=executor,
            correlation_id="run-input-error",
        )

    assert executed is False
    assert captured.value.boundary == "input"
    assert captured.value.missing_fields == ("request",)
    assert [event.phase for event in events] == ["STARTED", "FAILED"]
    assert events[-1].error_type == "AgentHarnessContractError"


def test_harness_rejects_missing_output_contract():
    events = []
    harness = _harness(events)

    with pytest.raises(AgentHarnessContractError) as captured:
        harness.execute(
            operation="run",
            input_value={"request": "value"},
            executor=lambda _: {},
        )

    assert captured.value.boundary == "output"
    assert captured.value.missing_fields == ("result",)
    assert events[-1].phase == "FAILED"


def test_harness_preserves_agent_exception_without_full_run_retry():
    events = []
    harness = _harness(events)
    attempts = 0

    def executor(_):
        nonlocal attempts
        attempts += 1
        raise LookupError("agent failure")

    with pytest.raises(LookupError, match="agent failure"):
        harness.execute(
            operation="run",
            input_value={"request": "value"},
            executor=executor,
        )

    assert attempts == 1
    assert events[-1].phase == "FAILED"
    assert events[-1].error_type == "LookupError"


def test_harness_rejects_unknown_operation():
    harness = _harness([])

    with pytest.raises(ValueError, match="Unsupported test-agent harness operation"):
        harness.execute(
            operation="unknown",
            input_value={"request": "value"},
            executor=lambda _: {"result": "ok"},
        )


def test_harness_runs_bounded_validation_repair_loop():
    events = []
    repair_calls = []
    harness = AgentHarness(
        agent_id="verified-agent",
        contracts={
            "run": HarnessContract(
                required_inputs=("request",),
                required_outputs=("result",),
                validator=lambda _input, output: (
                    () if output["result"] == "valid" else ("RESULT_INVALID",)
                ),
                max_validation_attempts=2,
            )
        },
        event_sink=events.append,
    )

    result = harness.execute(
        operation="run",
        input_value={"request": "value"},
        executor=lambda _: {"result": "invalid"},
        repair_executor=lambda _input, _output, issues, attempt: (
            repair_calls.append((issues, attempt)) or {"result": "valid"}
        ),
        correlation_id="verified-run",
    )

    assert result == {"result": "valid"}
    assert repair_calls == [(('RESULT_INVALID',), 1)]
    assert [event.phase for event in events] == [
        "STARTED",
        "VALIDATION_FAILED",
        "REPAIR_STARTED",
        "SUCCEEDED",
    ]
    assert events[-1].validation_attempt == 2


def test_harness_fails_after_validation_attempts_are_exhausted():
    events = []
    harness = AgentHarness(
        agent_id="verified-agent",
        contracts={
            "run": HarnessContract(
                required_inputs=("request",),
                required_outputs=("result",),
                validator=lambda _input, _output: ("RESULT_INVALID",),
                max_validation_attempts=2,
            )
        },
        event_sink=events.append,
    )

    with pytest.raises(AgentHarnessValidationError) as captured:
        harness.execute(
            operation="run",
            input_value={"request": "value"},
            executor=lambda _: {"result": "invalid"},
            repair_executor=lambda _input, output, _issues, _attempt: output,
        )

    assert captured.value.issue_codes == ("RESULT_INVALID",)
    assert captured.value.attempts == 2
    assert [event.phase for event in events] == [
        "STARTED",
        "VALIDATION_FAILED",
        "REPAIR_STARTED",
        "VALIDATION_FAILED",
        "FAILED",
    ]


def _trend_result(count: int) -> SimpleNamespace:
    ranking = pd.DataFrame(
        [
            {
                "challenge_id": f"trend-{index}",
                "is_social_challenge": True,
                "representative_youtube_url": (
                    f"https://www.youtube.com/watch?v=N{index:010d}"
                ),
                "guide_youtube_url": f"https://www.youtube.com/watch?v=N{index:010d}",
            }
            for index in range(1, count + 1)
        ]
    )
    return SimpleNamespace(
        run_id="top-100-run",
        ranking=ranking,
        source_metrics=pd.DataFrame(),
        statuses={},
    )


def test_trend_harness_accepts_complete_url_backed_top_one_hundred():
    config = {
        "paths": {},
        "ranking": {"top_n": 100, "require_youtube_video": True},
        "sources": {},
    }

    result = challenge_ranking_harness.execute(
        operation="research",
        input_value=config,
        executor=lambda _: _trend_result(100),
    )

    assert len(result.ranking) == 100


def test_trend_harness_rejects_incomplete_top_one_hundred():
    config = {
        "paths": {},
        "ranking": {"top_n": 100, "require_youtube_video": True},
        "sources": {},
    }

    with pytest.raises(AgentHarnessValidationError) as captured:
        challenge_ranking_harness.execute(
            operation="research",
            input_value=config,
            executor=lambda _: _trend_result(99),
        )

    assert captured.value.issue_codes == ("INSUFFICIENT_VALID_RANKED_TRENDS",)
