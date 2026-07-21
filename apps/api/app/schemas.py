from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field


SnapshotPolicy = Literal["exact", "previous_ready"]


class SnapshotResponse(BaseModel):
    id: str
    valuation_date: date
    status: str
    quote_count: int
    checksum: str | None
    created_at: datetime
    updated_at: datetime


class QuoteResponse(BaseModel):
    id: int
    valuation_date: date
    canonical_id: str
    asset_class: str
    quote_type: str
    currency: str | None
    unit: str
    raw_value: Decimal
    normalized_value: Decimal
    conversion: str
    factor: Decimal
    shift: Decimal
    source: str
    source_symbol: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class IngestionResponse(BaseModel):
    run_id: str
    snapshot_id: str
    valuation_date: date
    provider: str
    status: str
    rows_received: int
    rows_inserted: int
    rows_updated: int
    rows_unchanged: int
    quote_count: int
    checksum: str
    warnings: list[str]


class SnapshotResolutionResponse(BaseModel):
    as_of_date: date
    market_data_date: date
    snapshot_policy: SnapshotPolicy
    exact_snapshot: bool
    snapshot: SnapshotResponse


class AsOfQuotesResponse(SnapshotResolutionResponse):
    quotes: list[QuoteResponse]


class ZeroCouponRequest(BaseModel):
    as_of_date: date = Field(
        validation_alias=AliasChoices("as_of_date", "valuation_date"),
        description="Requested valuation/as-of date.",
    )
    maturity_date: date
    notional: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=12)
    discount_rate_quote_id: str = Field(min_length=1, max_length=160)
    compounding: Literal["simple", "annual", "continuous"] = "continuous"
    snapshot_policy: SnapshotPolicy = Field(
        default="exact",
        description=(
            "exact requires a READY snapshot on the requested date; previous_ready uses "
            "the latest READY snapshot on or before the requested date."
        ),
    )


class ZeroCouponResponse(BaseModel):
    as_of_date: date
    market_data_date: date
    snapshot_policy: SnapshotPolicy
    exact_snapshot: bool
    maturity_date: date
    currency: str
    notional: Decimal
    discount_rate_quote_id: str
    discount_rate: Decimal
    year_fraction_act_365f: Decimal
    compounding: str
    discount_factor: Decimal
    present_value: Decimal
    snapshot_checksum: str
