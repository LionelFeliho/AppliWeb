from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .schemas import SnapshotPolicy


DayCountName = Literal["ACT/360", "ACT/365F", "30E/360"]
DiscountingModeName = Literal["native", "base_collateral", "quote_collateral"]


class CurveNodeRequest(BaseModel):
    tenor: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)?[DWMYdwmy]$")
    quote_id: str = Field(min_length=1, max_length=160)


class CurveRequest(BaseModel):
    currency: str = Field(min_length=3, max_length=3)
    nodes: list[CurveNodeRequest] = Field(
        default_factory=list,
        description=(
            "OIS zero-rate quote IDs. When empty, canonical RATE.<CCY>.OIS.<TENOR> "
            "nodes are used for 1M, 3M, 6M, 1Y, 2Y, 5Y and 10Y."
        ),
    )
    required_tenors: list[str] = Field(
        default_factory=lambda: ["1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y"]
    )
    strict_completeness: bool = True


class XccySimulationRequest(BaseModel):
    paths: int = Field(default=512, ge=32, le=5000)
    seed: int = 42
    fx_volatility: float = Field(default=0.10, ge=0.0, le=2.0)
    fx_vol_quote_id: str | None = None
    base_rate_volatility: float = Field(default=0.01, ge=0.0, le=0.25)
    quote_rate_volatility: float = Field(default=0.01, ge=0.0, le=0.25)
    fx_base_rate_correlation: float = Field(default=-0.20, gt=-1.0, lt=1.0)
    fx_quote_rate_correlation: float = Field(default=0.20, gt=-1.0, lt=1.0)
    base_quote_rate_correlation: float = Field(default=0.50, gt=-1.0, lt=1.0)
    pfe_quantile: float = Field(default=0.95, gt=0.5, lt=1.0)
    collateral_threshold: float = Field(default=1_000_000.0, ge=0.0)
    minimum_transfer_amount: float = Field(default=0.0, ge=0.0)


class XvaAssumptionsRequest(BaseModel):
    counterparty_spread_bps: float = Field(default=100.0, ge=0.0)
    counterparty_spread_quote_id: str | None = None
    own_spread_bps: float = Field(default=80.0, ge=0.0)
    own_spread_quote_id: str | None = None
    counterparty_recovery: float = Field(default=0.40, ge=0.0, lt=1.0)
    own_recovery: float = Field(default=0.40, ge=0.0, lt=1.0)
    funding_spread_bps: float = Field(default=50.0, ge=0.0)
    collateral_spread_bps: float = Field(default=0.0, ge=-1000.0, le=1000.0)


class XccySwapRequest(BaseModel):
    as_of_date: date
    maturity_date: date
    snapshot_policy: SnapshotPolicy = "exact"
    base_currency: str = Field(default="EUR", min_length=3, max_length=3)
    quote_currency: str = Field(default="USD", min_length=3, max_length=3)
    reporting_currency: str | None = Field(
        default=None,
        description="Must be base or quote currency. Defaults to quote currency.",
    )
    notional_base: float = Field(default=10_000_000.0, gt=0.0)
    notional_quote: float | None = Field(
        default=None,
        gt=0.0,
        description="Defaults to notional_base multiplied by resolved FX spot.",
    )
    pay_base: bool = True
    base_spread_bps: float = 0.0
    quote_spread_bps: float = 0.0
    payment_frequency_months: int = Field(default=3, ge=1, le=12)
    base_day_count: DayCountName | None = None
    quote_day_count: DayCountName | None = None
    exchange_notionals: bool = True
    fx_spot_quote_id: str | None = Field(
        default=None,
        description=(
            "Optional explicit direct or inverse spot quote. If omitted, the resolver tries "
            "direct, inverse and USD-cross canonical FX quotes."
        ),
    )
    base_curve: CurveRequest | None = None
    quote_curve: CurveRequest | None = None
    cross_currency_basis_bps: float = Field(default=0.0, ge=-5000.0, le=5000.0)
    discounting_mode: DiscountingModeName = "native"
    compare_discounting: bool = True
    simulation: XccySimulationRequest = Field(default_factory=XccySimulationRequest)
    xva: XvaAssumptionsRequest = Field(default_factory=XvaAssumptionsRequest)

    @model_validator(mode="after")
    def validate_trade(self) -> "XccySwapRequest":
        self.base_currency = self.base_currency.upper()
        self.quote_currency = self.quote_currency.upper()
        if self.base_currency == self.quote_currency:
            raise ValueError("base_currency and quote_currency must differ")
        if self.maturity_date <= self.as_of_date:
            raise ValueError("maturity_date must be after as_of_date")
        if self.reporting_currency is not None:
            self.reporting_currency = self.reporting_currency.upper()
            if self.reporting_currency not in {self.base_currency, self.quote_currency}:
                raise ValueError("reporting_currency must equal base_currency or quote_currency")
        return self


class CurveNodeResponse(BaseModel):
    tenor: str
    time: float
    zero_rate: float
    discount_factor: float
    quote_id: str


class CurveResponse(BaseModel):
    currency: str
    interpolation: str
    nodes: list[CurveNodeResponse]
    missing_required_tenors: list[str]


class ExposurePointResponse(BaseModel):
    date: date
    time: float
    forward_mtm: float
    epe: float
    ene: float
    pfe: float
    expected_collateral: float


class XvaMetricsResponse(BaseModel):
    cva: float
    dva: float
    fva: float
    colva: float
    total_xva: float
    xva_adjusted_pv: float


class CrossGammaResponse(BaseModel):
    fx_bump_relative: float
    rate_bump_decimal: float
    fx_base_discount_interaction_pnl: float
    fx_quote_discount_interaction_pnl: float
    fx_base_discount_cross_gamma: float
    fx_quote_discount_cross_gamma: float


class XccySwapResponse(BaseModel):
    as_of_date: date
    market_data_date: date
    snapshot_policy: SnapshotPolicy
    exact_snapshot: bool
    snapshot_checksum: str
    base_currency: str
    quote_currency: str
    reporting_currency: str
    fx_spot: float
    fx_spot_quote_ids: list[str]
    fx_resolution: str
    notional_base: float
    notional_quote: float
    selected_discounting_mode: DiscountingModeName
    clean_pv: float
    pv_by_discounting: dict[str, float]
    discount_switch_impact: dict[str, float]
    cross_gamma: CrossGammaResponse
    base_curve: CurveResponse
    quote_curve: CurveResponse
    exposure_profile: list[ExposurePointResponse]
    xva: XvaMetricsResponse
    assumptions: dict[str, Any]
    warnings: list[str]
