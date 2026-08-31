from __future__ import annotations

import copy
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.store_trade_area_insight import StoreTradeAreaInsight
from app.schemas.trade_area_insight import StoreInfo, TradeAreaInsightResponse


def normalize_store_address(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", "", normalized).strip()


def save_store_trade_area_insight(
    db: Session,
    *,
    store: StoreInfo,
    result: TradeAreaInsightResponse,
) -> StoreTradeAreaInsight | None:
    normalized_address = normalize_store_address(store.address)
    if not normalized_address:
        return None
    record = db.get(StoreTradeAreaInsight, normalized_address)
    if record is None:
        record = StoreTradeAreaInsight(
            normalized_address=normalized_address,
            address=store.address,
        )
        db.add(record)
    record.address = store.address
    record.store_name = store.name
    record.latitude = store.latitude
    record.longitude = store.longitude
    record.district_name = result.district_name
    record.result = result.model_dump(mode="json", by_alias=True)
    record.analyzed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(record)
    return record


def enrich_store_context_with_trade_area(
    db: Session,
    store_context: dict[str, Any],
) -> dict[str, Any]:
    enriched = copy.deepcopy(store_context)
    store = dict(enriched.get("store") or {})
    location = dict(store.get("location") or {})
    normalized_address = normalize_store_address(location.get("address"))
    if not normalized_address:
        return enriched
    record = db.get(StoreTradeAreaInsight, normalized_address)
    if record is None:
        return enriched
    trade_area = dict(enriched.get("trade_area") or {})
    if record.district_name:
        trade_area["district_name"] = record.district_name
    summary = str((record.result or {}).get("summary") or "").strip()
    if summary:
        trade_area["summary"] = summary
    trade_area["analysis_result"] = dict(record.result or {})
    enriched["trade_area"] = trade_area
    return enriched
