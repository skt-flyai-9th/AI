from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class ConnectorResult:
    source: str
    metrics: pd.DataFrame
    status: dict[str, Any] = field(default_factory=dict)
    raw_rows: pd.DataFrame | None = None

    @property
    def successful(self) -> bool:
        return bool(self.status.get("success", False))
