from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any


class AssetClass(str, Enum):
    RATES = "RATES"
    FIXED_INCOME = "FIXED_INCOME"
    FX = "FX"
    CREDIT = "CREDIT"
    EQUITY = "EQUITY"
    COMMODITY = "COMMODITY"


class QuoteType(str, Enum):
    SPOT = "SPOT"
    RATE = "RATE"
    YIELD = "YIELD"
    PRICE = "PRICE"
    SPREAD = "SPREAD"
    VOLATILITY = "VOLATILITY"
    FORWARD = "FORWARD"
    DISCOUNT_FACTOR = "DISCOUNT_FACTOR"


class ConversionKind(str, Enum):
    IDENTITY = "identity"
    LINEAR = "linear"
    PERCENT_TO_DECIMAL = "percent_to_decimal"
    BPS_TO_DECIMAL = "bps_to_decimal"
    FUTURES_PRICE_TO_RATE = "futures_price_to_rate"
    INVERSE = "inverse"
    FX_PIPS = "fx_pips"
    CENTS_TO_UNIT = "cents_to_unit"


@dataclass(frozen=True)
class RawQuote:
    valuation_date: date
    canonical_id: str
    asset_class: AssetClass
    quote_type: QuoteType
    raw_value: Decimal
    source: str
    source_symbol: str
    conversion: ConversionKind = ConversionKind.IDENTITY
    factor: Decimal = Decimal("1")
    shift: Decimal = Decimal("0")
    currency: str | None = None
    unit: str = "DECIMAL"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EcbSeriesSpec:
    flow: str
    key: str
    canonical_id: str
    asset_class: AssetClass
    quote_type: QuoteType
    currency: str | None
    unit: str
    conversion: ConversionKind = ConversionKind.IDENTITY
    factor: Decimal = Decimal("1")
    shift: Decimal = Decimal("0")


class MarketDataError(Exception):
    """Base exception for market-data failures."""


class MarketDataValidationError(MarketDataError):
    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else errors
        super().__init__("; ".join(self.errors))


class MarketDataConflictError(MarketDataError):
    pass


class ProviderError(MarketDataError):
    pass
