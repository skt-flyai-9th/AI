from __future__ import annotations

from pydantic import BaseModel, Field


class StoreInfo(BaseModel):
    name: str
    category: str
    sub_category: str | None = None
    address: str
    latitude: float | None = None
    longitude: float | None = None


class TradeAreaInsightRequest(BaseModel):
    store: StoreInfo


class AgeDistribution(BaseModel):
    """Five age buckets. Values are percentages that must sum to 100."""

    field_10s: int = Field(alias="10s")
    field_20s: int = Field(alias="20s")
    field_30s: int = Field(alias="30s")
    field_40s: int = Field(alias="40s")
    field_50s_plus: int = Field(alias="50s_plus")

    model_config = {"populate_by_name": True}


class GenderDistribution(BaseModel):
    """Two gender buckets. Values are percentages that must sum to 100."""

    male: int
    female: int


class TradeAreaInsightResponse(BaseModel):
    district_name: str | None
    summary: str
    age_distribution: AgeDistribution
    gender_distribution: GenderDistribution
