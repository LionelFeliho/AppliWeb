from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Iterable

from .conventions import year_fraction


_TENOR_RE = re.compile(r"^(ON|TN|SN|\d+[DWMY])$", re.IGNORECASE)
_CORE_TENORS = ("1Y", "2Y", "5Y", "10Y")
_OVERNIGHT_IDS: dict[str, tuple[str, ...]] = {
    "USD": ("RATE.USD.SOFR.ON",),
    "EUR": ("RATE.EUR.ESTR.ON",),
    "GBP": ("RATE.GBP.SONIA.ON",),
    "JPY": ("RATE.JPY.TONA.ON",),
    "CHF": ("RATE.CHF.SARON.ON",),
    "CAD": ("RATE.CAD.CORRA.ON",),
    "AUD": ("RATE.AUD.AONIA.ON",),
    "NZD": ("RATE.NZD.NZIONA.ON",),
    "SEK": ("RATE.SEK.SWESTR.ON",),
    "NOK": ("RATE.NOK.NOWA.ON",),
}


@dataclass(frozen=True)
class CurvePoint:
    tenor: str
    years: float
    zero_rate: float
    canonical_id: str
    source: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def tenor_to_years(tenor: str) -> float:
    normalized = tenor.strip().upper()
    if normalized in {"ON", "TN", "SN"}:
        return 1.0 / 365.0
    if not _TENOR_RE.fullmatch(normalized):
        raise ValueError(f"Unsupported tenor: {tenor}")
    amount = int(normalized[:-1])
    unit = normalized[-1]
    if unit == "D":
        return amount / 365.0
    if unit == "W":
        return amount * 7.0 / 365.0
    if unit == "M":
        return amount / 12.0
    return float(amount)


class DiscountCurve:
    """Piecewise-linear zero-rate curve with continuous compounding.

    This is intentionally a transparent EOD pillar curve, not a full deposit/OIS
    instrument bootstrap. The canonical market contract is stable while the curve
    builder can later be replaced with a QuantLib/ORE-validated implementation.
    """

    def __init__(self, as_of_date: date, currency: str, points: Iterable[CurvePoint]):
        unique: dict[float, CurvePoint] = {}
        for point in points:
            unique[point.years] = point
        self.points = tuple(sorted(unique.values(), key=lambda item: item.years))
        if len(self.points) < 2:
            raise ValueError(
                f"{currency} OIS curve requires at least two distinct tenor pillars"
            )
        self.as_of_date = as_of_date
        self.currency = currency.upper()

    @property
    def max_years(self) -> float:
        return self.points[-1].years

    def zero_rate(self, years: float, parallel_shift: float = 0.0) -> float:
        t = max(float(years), 0.0)
        if t <= self.points[0].years:
            return self.points[0].zero_rate + parallel_shift
        if t >= self.points[-1].years:
            return self.points[-1].zero_rate + parallel_shift
        for left, right in zip(self.points, self.points[1:]):
            if left.years <= t <= right.years:
                weight = (t - left.years) / (right.years - left.years)
                return (
                    left.zero_rate
                    + weight * (right.zero_rate - left.zero_rate)
                    + parallel_shift
                )
        raise RuntimeError("Curve interpolation failed")  # pragma: no cover

    def discount_factor(self, years: float, parallel_shift: float = 0.0) -> float:
        t = max(float(years), 0.0)
        return math.exp(-self.zero_rate(t, parallel_shift) * t)

    def discount_between(
        self, start_years: float, end_years: float, parallel_shift: float = 0.0
    ) -> float:
        if end_years < start_years:
            raise ValueError("end_years must be greater than or equal to start_years")
        start_df = self.discount_factor(start_years, parallel_shift)
        end_df = self.discount_factor(end_years, parallel_shift)
        return end_df / start_df

    def forward_rate(
        self,
        start_years: float,
        end_years: float,
        accrual_year_fraction: float,
        parallel_shift: float = 0.0,
    ) -> float:
        if accrual_year_fraction <= 0:
            raise ValueError("Accrual year fraction must be positive")
        forward_df = self.discount_between(start_years, end_years, parallel_shift)
        return (1.0 / forward_df - 1.0) / accrual_year_fraction

    def pillars(self) -> list[dict[str, object]]:
        return [point.as_dict() for point in self.points]


def _tenor_from_quote(quote: dict[str, Any], currency: str) -> str | None:
    metadata = quote.get("metadata") or {}
    metadata_tenor = str(metadata.get("tenor") or "").strip().upper()
    if metadata_tenor and _TENOR_RE.fullmatch(metadata_tenor):
        return metadata_tenor

    canonical_id = str(quote.get("canonical_id") or "").upper()
    prefix = f"RATE.{currency}.OIS."
    if canonical_id.startswith(prefix):
        candidate = canonical_id[len(prefix) :].split(".", 1)[0]
        return candidate if _TENOR_RE.fullmatch(candidate) else None
    if canonical_id in _OVERNIGHT_IDS.get(currency, ()):
        return "ON"
    return None


def curve_points_from_quotes(
    quotes: Iterable[dict[str, Any]], currency: str
) -> list[CurvePoint]:
    code = currency.upper()
    points: list[CurvePoint] = []
    for quote in quotes:
        if quote.get("quote_type") not in {"RATE", "YIELD"}:
            continue
        tenor = _tenor_from_quote(quote, code)
        if tenor is None:
            continue
        quote_currency = str(quote.get("currency") or code).upper()
        if quote_currency != code:
            continue
        points.append(
            CurvePoint(
                tenor=tenor,
                years=tenor_to_years(tenor),
                zero_rate=float(quote["normalized_value"]),
                canonical_id=str(quote["canonical_id"]),
                source=str(quote.get("source") or "UNKNOWN"),
            )
        )
    return sorted(points, key=lambda point: point.years)


def build_ois_curve(
    as_of_date: date, quotes: Iterable[dict[str, Any]], currency: str
) -> DiscountCurve:
    return DiscountCurve(as_of_date, currency, curve_points_from_quotes(quotes, currency))


def curve_completeness(
    quotes: Iterable[dict[str, Any]], currency: str
) -> dict[str, object]:
    points = curve_points_from_quotes(quotes, currency)
    available = {point.tenor for point in points}
    missing = [tenor for tenor in _CORE_TENORS if tenor not in available]
    return {
        "currency": currency.upper(),
        "ready": len(points) >= 2 and not missing,
        "available_tenors": [point.tenor for point in points],
        "missing_core_tenors": missing,
        "pillars": [point.as_dict() for point in points],
    }


def date_to_curve_time(as_of_date: date, target_date: date) -> float:
    return max(year_fraction(as_of_date, target_date, "ACT/365F"), 0.0)
