from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from time import perf_counter
from typing import Any, Literal, TypeVar


logger = logging.getLogger(__name__)

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")
HarnessEventPhase = Literal[
    "STARTED",
    "VALIDATION_FAILED",
    "REPAIR_STARTED",
    "SUCCEEDED",
    "FAILED",
]
HarnessValidator = Callable[[Any, Any], tuple[str, ...]]


@dataclass(frozen=True)
class HarnessContract:
    """Minimal input/output boundary for one existing Agent operation."""

    required_inputs: tuple[str, ...]
    required_outputs: tuple[str, ...]
    validator: HarnessValidator | None = None
    max_validation_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_validation_attempts < 1:
            raise ValueError("max_validation_attempts must be at least 1")


@dataclass(frozen=True)
class AgentRunContext:
    agent_id: str
    operation: str
    correlation_id: str
    invocation_id: str


@dataclass(frozen=True)
class AgentHarnessEvent:
    phase: HarnessEventPhase
    context: AgentRunContext
    duration_ms: int | None = None
    error_type: str | None = None
    validation_attempt: int | None = None
    issue_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(payload.pop("context"))
        return payload


class AgentHarnessContractError(RuntimeError):
    def __init__(
        self,
        *,
        agent_id: str,
        operation: str,
        boundary: Literal["input", "output"],
        missing_fields: tuple[str, ...],
    ) -> None:
        fields = ", ".join(missing_fields)
        super().__init__(
            f"{agent_id}.{operation} {boundary} contract is missing required fields: {fields}"
        )
        self.agent_id = agent_id
        self.operation = operation
        self.boundary = boundary
        self.missing_fields = missing_fields


class AgentHarnessValidationError(RuntimeError):
    def __init__(
        self,
        *,
        agent_id: str,
        operation: str,
        issue_codes: tuple[str, ...],
        attempts: int,
    ) -> None:
        issues = ", ".join(issue_codes)
        super().__init__(
            f"{agent_id}.{operation} validation failed after {attempts} attempt(s): {issues}"
        )
        self.agent_id = agent_id
        self.operation = operation
        self.issue_codes = issue_codes
        self.attempts = attempts


HarnessEventSink = Callable[[AgentHarnessEvent], None]
RepairExecutor = Callable[[InputT, OutputT, tuple[str, ...], int], OutputT]


class AgentHarness:
    """Shared execution boundary around the three existing Agent implementations.

    The harness validates operation contracts and emits lifecycle metadata. It
    deliberately does not retry a whole Agent execution or own database state.
    Retries, checkpoints, transactions, and fallbacks remain in their current
    Agent/service layers.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        contracts: Mapping[str, HarnessContract],
        event_sink: HarnessEventSink | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.contracts = dict(contracts)
        self.event_sink = event_sink

    def execute(
        self,
        *,
        operation: str,
        input_value: InputT,
        executor: Callable[[InputT], OutputT],
        correlation_id: str | None = None,
        repair_executor: RepairExecutor[InputT, OutputT] | None = None,
    ) -> OutputT:
        contract = self.contracts.get(operation)
        if contract is None:
            raise ValueError(f"Unsupported {self.agent_id} harness operation: {operation}")

        invocation_id = str(uuid.uuid4())
        context = AgentRunContext(
            agent_id=self.agent_id,
            operation=operation,
            correlation_id=correlation_id or invocation_id,
            invocation_id=invocation_id,
        )
        started_at = perf_counter()
        self._emit(AgentHarnessEvent(phase="STARTED", context=context))

        try:
            self._validate_contract(
                operation=operation,
                boundary="input",
                value=input_value,
                required_fields=contract.required_inputs,
            )
            result = executor(input_value)
            result, validation_attempt = self._validate_with_repair(
                operation=operation,
                contract=contract,
                input_value=input_value,
                result=result,
                repair_executor=repair_executor,
                context=context,
                started_at=started_at,
            )
        except Exception as exc:
            self._emit(
                AgentHarnessEvent(
                    phase="FAILED",
                    context=context,
                    duration_ms=_elapsed_ms(started_at),
                    error_type=type(exc).__name__,
                )
            )
            raise

        self._emit(
            AgentHarnessEvent(
                phase="SUCCEEDED",
                context=context,
                duration_ms=_elapsed_ms(started_at),
                validation_attempt=validation_attempt,
            )
        )
        return result

    def _validate_with_repair(
        self,
        *,
        operation: str,
        contract: HarnessContract,
        input_value: InputT,
        result: OutputT,
        repair_executor: RepairExecutor[InputT, OutputT] | None,
        context: AgentRunContext,
        started_at: float,
    ) -> tuple[OutputT, int]:
        validation_attempt = 1
        while True:
            contract_error: AgentHarnessContractError | None = None
            issue_codes: tuple[str, ...] = ()
            try:
                self._validate_contract(
                    operation=operation,
                    boundary="output",
                    value=result,
                    required_fields=contract.required_outputs,
                )
            except AgentHarnessContractError as exc:
                contract_error = exc
                issue_codes = tuple(f"MISSING_OUTPUT_{field.upper()}" for field in exc.missing_fields)

            if contract_error is None and contract.validator is not None:
                issue_codes = tuple(dict.fromkeys(contract.validator(input_value, result)))

            if not issue_codes:
                return result, validation_attempt

            self._emit(
                AgentHarnessEvent(
                    phase="VALIDATION_FAILED",
                    context=context,
                    duration_ms=_elapsed_ms(started_at),
                    validation_attempt=validation_attempt,
                    issue_codes=issue_codes,
                )
            )
            if (
                repair_executor is None
                or validation_attempt >= contract.max_validation_attempts
            ):
                if contract_error is not None:
                    raise contract_error
                raise AgentHarnessValidationError(
                    agent_id=self.agent_id,
                    operation=operation,
                    issue_codes=issue_codes,
                    attempts=validation_attempt,
                )

            self._emit(
                AgentHarnessEvent(
                    phase="REPAIR_STARTED",
                    context=context,
                    duration_ms=_elapsed_ms(started_at),
                    validation_attempt=validation_attempt + 1,
                    issue_codes=issue_codes,
                )
            )
            result = repair_executor(
                input_value,
                result,
                issue_codes,
                validation_attempt,
            )
            validation_attempt += 1

    def _validate_contract(
        self,
        *,
        operation: str,
        boundary: Literal["input", "output"],
        value: Any,
        required_fields: tuple[str, ...],
    ) -> None:
        missing = tuple(field for field in required_fields if not _has_field(value, field))
        if missing:
            raise AgentHarnessContractError(
                agent_id=self.agent_id,
                operation=operation,
                boundary=boundary,
                missing_fields=missing,
            )

    def _emit(self, event: AgentHarnessEvent) -> None:
        log = logger.error if event.phase == "FAILED" else logger.info
        log("agent_harness_event", extra={"agent_harness_event": event.as_dict()})
        if self.event_sink is None:
            return
        try:
            self.event_sink(event)
        except Exception:
            logger.exception("agent_harness_event_sink_failed")


def _has_field(value: Any, field: str) -> bool:
    if isinstance(value, Mapping):
        return field in value and value[field] is not None
    return hasattr(value, field) and getattr(value, field) is not None


def _elapsed_ms(started_at: float) -> int:
    return max(0, int((perf_counter() - started_at) * 1000))
