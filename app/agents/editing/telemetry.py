from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EditingUsage:
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


_usage: ContextVar[EditingUsage | None] = ContextVar("editing_usage", default=None)


def _current() -> EditingUsage:
    return _usage.get() or EditingUsage()


def reset_usage() -> None:
    _usage.set(EditingUsage())


def record_request() -> None:
    current = _current()
    _usage.set(
        EditingUsage(
            request_count=current.request_count + 1,
            input_tokens=current.input_tokens,
            output_tokens=current.output_tokens,
        )
    )


def record_response(payload: dict[str, Any]) -> None:
    raw = payload.get("usage") or {}
    current = _current()
    _usage.set(
        EditingUsage(
            request_count=current.request_count,
            input_tokens=current.input_tokens + int(raw.get("input_tokens") or 0),
            output_tokens=current.output_tokens + int(raw.get("output_tokens") or 0),
        )
    )


def usage_snapshot() -> EditingUsage:
    return _current()
