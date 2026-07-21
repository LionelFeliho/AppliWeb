from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .schemas import SnapshotPolicy


class CrossSectionCalibrationSettings(BaseModel):
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    tenors: list[str] = Field(default_factory=list)
    minimum_observations_per_tenor: int = Field(default=8, ge=4)
    minimum_contributors: int = Field(default=0, ge=0)
    ridge: float = Field(default=1e-8, ge=0.0, le=1.0)
    source: str | None = None
    quote_id_prefix: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "CrossSectionCalibrationSettings":
        if self.currency:
            self.currency = self.currency.upper()
        self.tenors = [tenor.upper() for tenor in self.tenors]
        return self


class CreditBasketConstituentRequest(BaseModel):
    sector: str
    region: str
    rating: str
    seniority: str = "SENIOR"
    weight: float = Field(default=1.0, gt=0.0)
    name: str | None = None


class NomuraCrossSectionCalibrationRequest(BaseModel):
    as_of_date: date
    snapshot_policy: SnapshotPolicy = "exact"
    settings: CrossSectionCalibrationSettings = Field(
        default_factory=CrossSectionCalibrationSettings
    )


class NomuraBasketCurveRequest(NomuraCrossSectionCalibrationRequest):
    basket: list[CreditBasketConstituentRequest] = Field(min_length=1)
    recovery_rate: float = Field(default=0.40, ge=0.0, lt=1.0)
    aggregation: Literal["arithmetic", "geometric"] = "arithmetic"
    missing_category_policy: Literal["error", "global"] = "error"
    include_constituent_spreads: bool = False


class CreditExposurePointRequest(BaseModel):
    time: float = Field(ge=0.0)
    epe: float = Field(default=0.0, ge=0.0)
    ene: float = Field(default=0.0, le=0.0)


class NomuraCrossSectionXvaRequest(NomuraCrossSectionCalibrationRequest):
    counterparty_basket: list[CreditBasketConstituentRequest] = Field(min_length=1)
    own_basket: list[CreditBasketConstituentRequest] = Field(default_factory=list)
    counterparty_recovery_rate: float = Field(default=0.40, ge=0.0, lt=1.0)
    own_recovery_rate: float = Field(default=0.40, ge=0.0, lt=1.0)
    aggregation: Literal["arithmetic", "geometric"] = "arithmetic"
    missing_category_policy: Literal["error", "global"] = "error"
    discount_rate: float = Field(default=0.0, ge=-0.25, le=1.0)
    exposure_profile: list[CreditExposurePointRequest] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_times(self) -> "NomuraCrossSectionXvaRequest":
        times = [point.time for point in self.exposure_profile]
        if times != sorted(times) or len(set(times)) != len(times):
            raise ValueError("exposure_profile times must be strictly increasing")
        return self


class NomuraCrossSectionResponse(BaseModel):
    as_of_date: date
    market_data_date: date
    snapshot_policy: SnapshotPolicy
    exact_snapshot: bool
    snapshot_checksum: str
    model: dict[str, Any]
    tenor_fits: list[dict[str, Any]]
    observations_used: int
    quote_ids_used: list[str]
    warnings: list[str]


class NomuraBasketCurveResponse(NomuraCrossSectionResponse):
    curve: dict[str, Any]


class NomuraCrossSectionXvaResponse(NomuraCrossSectionResponse):
    counterparty_curve: dict[str, Any]
    own_curve: dict[str, Any] | None
    credit_xva: dict[str, Any]
