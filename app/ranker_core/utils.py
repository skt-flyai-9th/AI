from __future__ import annotations

import hashlib
import json
import math
import random
import re
import time
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime, timezone
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

T = TypeVar("T")
_KOREAN_RE = re.compile(r"[가-힣]")
_HTML_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any, *, default_tz: str = "Asia/Seoul") -> pd.Timestamp | pd.NaT:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return pd.NaT
    text = str(value).strip()
    if not text:
        return pd.NaT
    ts = pd.to_datetime(text, errors="coerce", utc=False)
    if pd.isna(ts):
        return pd.NaT
    if ts.tzinfo is None:
        ts = ts.tz_localize(ZoneInfo(default_tz))
    return ts.tz_convert("UTC")


def parse_now(value: Any, timezone_name: str) -> pd.Timestamp:
    if value in (None, "", "null"):
        return pd.Timestamp.now(tz="UTC")
    ts = parse_datetime(value, default_tz=timezone_name)
    if pd.isna(ts):
        raise ValueError(f"now 값을 날짜로 해석할 수 없습니다: {value}")
    return ts


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    text = text.replace("#", "")
    text = re.sub(r"[^0-9a-z가-힣]+", " ", text)
    return _SPACE_RE.sub(" ", text).strip()


def strip_html(value: Any) -> str:
    return _SPACE_RE.sub(" ", _HTML_RE.sub(" ", str(value or ""))).strip()


def has_korean(value: Any) -> bool:
    return bool(_KOREAN_RE.search(str(value or "")))


def korean_ratio(value: Any) -> float:
    text = str(value or "")
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return 0.0
    korean = sum(1 for ch in letters if "가" <= ch <= "힣")
    return korean / len(letters)


def parse_aliases(value: Any, fallback: str | None = None) -> list[str]:
    parts = [part.strip() for part in str(value or "").split("|") if part.strip()]
    if fallback and fallback.strip() and fallback.strip() not in parts:
        parts.insert(0, fallback.strip())
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        key = normalize_text(part)
        if key and key not in seen:
            seen.add(key)
            result.append(part)
    return result


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "t", "yes", "y", "paid", "sponsored"}


def clip01(value: float) -> float:
    return float(min(1.0, max(0.0, value)))


def smoothed_growth(current: float, previous: float, alpha: float = 2.0) -> float:
    return (float(current) + alpha) / (float(previous) + alpha) - 1.0


def normalized_entropy(values: Sequence[Any], weights: Sequence[float] | None = None) -> float:
    if not values:
        return 0.0
    frame = pd.DataFrame({"value": list(values)})
    if weights is None:
        frame["weight"] = 1.0
    else:
        frame["weight"] = [max(0.0, safe_float(w)) for w in weights]
    grouped = frame.groupby("value", dropna=False)["weight"].sum()
    total = float(grouped.sum())
    if total <= 0 or len(grouped) <= 1:
        return 0.0
    probs = grouped / total
    entropy = float(-(probs * np.log(probs)).sum())
    return clip01(entropy / math.log(len(grouped)))


def weighted_mean(pairs: Iterable[tuple[float | None, float]]) -> float:
    numerator = 0.0
    denominator = 0.0
    for value, weight in pairs:
        if value is None or not np.isfinite(value) or weight <= 0:
            continue
        numerator += float(value) * float(weight)
        denominator += float(weight)
    return numerator / denominator if denominator > 0 else 0.0


def chunks(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    if size <= 0:
        raise ValueError("chunk size는 1 이상이어야 합니다.")
    for index in range(0, len(items), size):
        yield items[index : index + size]


def stable_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def json_dumps(value: Any) -> str:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, (datetime, pd.Timestamp)):
        if pd.isna(value):
            return None
        return pd.Timestamp(value).isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    retry_statuses: set[int] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    retry_statuses = retry_statuses or {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            if response.status_code in retry_statuses and attempt < retries:
                delay = min(8.0, (2**attempt) + random.random())
                time.sleep(delay)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"JSON 객체 응답이 아닙니다: {url}")
            return payload
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            time.sleep(min(8.0, (2**attempt) + random.random()))
    raise RuntimeError(f"API 요청 실패: {method} {url}: {last_error}") from last_error
