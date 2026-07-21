from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Literal


DayCount = Literal["ACT/360", "ACT/365F", "30/360"]
BusinessDayConvention = Literal["following", "modified_following", "preceding"]


@dataclass(frozen=True)
class CurrencyConvention:
    code: str
    name: str
    overnight_index: str
    calendar_code: str
    day_count: DayCount
    fixed_leg_frequency_months: int
    floating_leg_frequency_months: int
    spot_lag_business_days: int
    business_day_convention: BusinessDayConvention = "modified_following"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


G10_CONVENTIONS: dict[str, CurrencyConvention] = {
    "USD": CurrencyConvention("USD", "US Dollar", "SOFR", "NYC", "ACT/360", 6, 3, 2),
    "EUR": CurrencyConvention("EUR", "Euro", "ESTR", "TARGET", "ACT/360", 12, 3, 2),
    "GBP": CurrencyConvention("GBP", "Pound Sterling", "SONIA", "LON", "ACT/365F", 12, 3, 0),
    "JPY": CurrencyConvention("JPY", "Japanese Yen", "TONA", "TKY", "ACT/365F", 6, 3, 2),
    "CHF": CurrencyConvention("CHF", "Swiss Franc", "SARON", "ZRH", "ACT/360", 12, 3, 2),
    "CAD": CurrencyConvention("CAD", "Canadian Dollar", "CORRA", "TOR", "ACT/365F", 6, 3, 1),
    "AUD": CurrencyConvention("AUD", "Australian Dollar", "AONIA", "SYD", "ACT/365F", 6, 3, 2),
    "NZD": CurrencyConvention("NZD", "New Zealand Dollar", "NZIONA", "WLG", "ACT/365F", 6, 3, 2),
    "SEK": CurrencyConvention("SEK", "Swedish Krona", "SWESTR", "STO", "ACT/360", 12, 3, 2),
    "NOK": CurrencyConvention("NOK", "Norwegian Krone", "NOWA", "OSL", "ACT/360", 12, 3, 2),
}

G10_CURRENCIES: tuple[str, ...] = tuple(G10_CONVENTIONS)


def currency_convention(currency: str) -> CurrencyConvention:
    code = currency.upper()
    try:
        return G10_CONVENTIONS[code]
    except KeyError as exc:
        raise ValueError(f"Unsupported G10 currency: {currency}") from exc


def is_business_day(value: date) -> bool:
    """Weekend-only MVP calendar.

    The registry carries the intended financial-centre calendar code so a holiday
    calendar implementation can replace this function without changing API contracts.
    """

    return value.weekday() < 5


def adjust_business_day(
    value: date, convention: BusinessDayConvention = "modified_following"
) -> date:
    if is_business_day(value):
        return value

    if convention == "preceding":
        adjusted = value
        while not is_business_day(adjusted):
            adjusted -= timedelta(days=1)
        return adjusted

    adjusted = value
    while not is_business_day(adjusted):
        adjusted += timedelta(days=1)
    if convention == "modified_following" and adjusted.month != value.month:
        adjusted = value
        while not is_business_day(adjusted):
            adjusted -= timedelta(days=1)
    return adjusted


def add_business_days(value: date, days: int) -> date:
    if days == 0:
        return adjust_business_day(value)
    direction = 1 if days > 0 else -1
    remaining = abs(days)
    current = value
    while remaining:
        current += timedelta(days=direction)
        if is_business_day(current):
            remaining -= 1
    return current


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - date(year, month, 1)).days


def add_months(value: date, months: int) -> date:
    total = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(value.day, _days_in_month(year, month))
    return date(year, month, day)


def year_fraction(start: date, end: date, basis: DayCount) -> float:
    if end < start:
        return -year_fraction(end, start, basis)
    if basis == "ACT/360":
        return (end - start).days / 360.0
    if basis == "ACT/365F":
        return (end - start).days / 365.0

    # US 30/360, sufficient for the first fixed-leg convention slice.
    d1 = min(start.day, 30)
    d2 = min(end.day, 30) if d1 == 30 else end.day
    return (
        360 * (end.year - start.year)
        + 30 * (end.month - start.month)
        + (d2 - d1)
    ) / 360.0


def generate_schedule(
    start: date,
    end: date,
    frequency_months: int,
    convention: BusinessDayConvention = "modified_following",
) -> list[date]:
    if end <= start:
        raise ValueError("Schedule end date must be after start date")
    if frequency_months <= 0 or 12 % frequency_months != 0:
        raise ValueError("Payment frequency must be a positive divisor of 12 months")

    dates: list[date] = []
    cursor = start
    while True:
        next_date = add_months(cursor, frequency_months)
        if next_date >= end:
            dates.append(adjust_business_day(end, convention))
            break
        dates.append(adjust_business_day(next_date, convention))
        cursor = next_date
    return dates


def g10_reference_payload() -> dict[str, object]:
    return {
        "currencies": [G10_CONVENTIONS[code].as_dict() for code in G10_CURRENCIES],
        "calendar_implementation": "weekend_only_mvp",
        "calendar_warning": (
            "Calendar codes are part of the contract, but the current implementation adjusts "
            "weekends only. Production holiday sets must be added before legal confirmation."
        ),
        "discounting_modes": ["native_ois", "base_collateral", "quote_collateral"],
        "curve_construction": "piecewise_linear_zero_rates_from_canonical_OIS_pillars",
    }
