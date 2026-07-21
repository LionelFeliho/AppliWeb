from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Iterable, Literal


DayCount = Literal["ACT/360", "ACT/365F", "30E/360"]
BusinessDayConvention = Literal["following", "modified_following", "preceding", "unadjusted"]


@dataclass(frozen=True)
class CurrencyConvention:
    currency: str
    ois_index: str
    calendar: str
    day_count: DayCount
    spot_lag_business_days: int
    default_payment_frequency_months: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


G10_CURRENCIES: dict[str, CurrencyConvention] = {
    "USD": CurrencyConvention("USD", "SOFR", "USNY", "ACT/360", 2, 3),
    "EUR": CurrencyConvention("EUR", "ESTR", "TARGET", "ACT/360", 2, 3),
    "GBP": CurrencyConvention("GBP", "SONIA", "GBLO", "ACT/365F", 2, 3),
    "JPY": CurrencyConvention("JPY", "TONAR", "JPTO", "ACT/365F", 2, 3),
    "CHF": CurrencyConvention("CHF", "SARON", "CHZU", "ACT/360", 2, 3),
    "CAD": CurrencyConvention("CAD", "CORRA", "CATO", "ACT/365F", 1, 3),
    "AUD": CurrencyConvention("AUD", "AONIA", "AUSY", "ACT/365F", 2, 3),
    "NZD": CurrencyConvention("NZD", "NZIONA", "NZAU", "ACT/365F", 2, 3),
    "NOK": CurrencyConvention("NOK", "NOWA", "NOOS", "ACT/365F", 2, 3),
    "SEK": CurrencyConvention("SEK", "SWESTR", "SEST", "ACT/360", 2, 3),
}

DEFAULT_CURVE_TENORS: tuple[str, ...] = ("1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y")


def currency_convention(currency: str) -> CurrencyConvention:
    code = currency.upper()
    try:
        return G10_CURRENCIES[code]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported currency {currency!r}. Supported G10 currencies: "
            f"{', '.join(sorted(G10_CURRENCIES))}"
        ) from exc


def parse_tenor_years(tenor: str) -> float:
    value = tenor.strip().upper()
    if len(value) < 2:
        raise ValueError(f"Invalid tenor {tenor!r}")
    try:
        amount = float(value[:-1])
    except ValueError as exc:
        raise ValueError(f"Invalid tenor {tenor!r}") from exc
    if amount <= 0:
        raise ValueError(f"Tenor must be positive: {tenor!r}")
    unit = value[-1]
    factors = {"D": 1.0 / 365.0, "W": 7.0 / 365.0, "M": 1.0 / 12.0, "Y": 1.0}
    if unit not in factors:
        raise ValueError(f"Unsupported tenor unit in {tenor!r}; use D, W, M or Y")
    return amount * factors[unit]


def year_fraction(start: date, end: date, convention: DayCount) -> float:
    if end < start:
        return -year_fraction(end, start, convention)
    if convention == "ACT/360":
        return (end - start).days / 360.0
    if convention == "ACT/365F":
        return (end - start).days / 365.0
    if convention == "30E/360":
        d1 = min(start.day, 30)
        d2 = min(end.day, 30)
        return ((end.year - start.year) * 360 + (end.month - start.month) * 30 + d2 - d1) / 360.0
    raise ValueError(f"Unsupported day-count convention: {convention}")


def add_months(value: date, months: int) -> date:
    if months <= 0:
        raise ValueError("months must be positive")
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def is_business_day(value: date, holidays: Iterable[date] = ()) -> bool:
    return value.weekday() < 5 and value not in set(holidays)


def adjust_business_day(
    value: date,
    convention: BusinessDayConvention = "modified_following",
    holidays: Iterable[date] = (),
) -> date:
    holiday_set = set(holidays)
    if convention == "unadjusted" or is_business_day(value, holiday_set):
        return value

    if convention in {"following", "modified_following"}:
        adjusted = value
        while not is_business_day(adjusted, holiday_set):
            adjusted += timedelta(days=1)
        if convention == "modified_following" and adjusted.month != value.month:
            return adjust_business_day(value, "preceding", holiday_set)
        return adjusted

    if convention == "preceding":
        adjusted = value
        while not is_business_day(adjusted, holiday_set):
            adjusted -= timedelta(days=1)
        return adjusted

    raise ValueError(f"Unsupported business-day convention: {convention}")


def generate_schedule(
    start: date,
    end: date,
    frequency_months: int,
    business_day_convention: BusinessDayConvention = "modified_following",
    holidays: Iterable[date] = (),
) -> list[date]:
    if end <= start:
        raise ValueError("schedule end must be after start")
    if frequency_months <= 0:
        raise ValueError("frequency_months must be positive")

    result: list[date] = []
    cursor = start
    while cursor < end:
        next_date = add_months(cursor, frequency_months)
        if next_date >= end:
            next_date = end
        adjusted = adjust_business_day(next_date, business_day_convention, holidays)
        if result and adjusted <= result[-1]:
            adjusted = next_date
        result.append(adjusted)
        cursor = next_date
    return result


def default_curve_quote_ids(currency: str) -> list[dict[str, str]]:
    code = currency_convention(currency).currency
    return [
        {"tenor": tenor, "quote_id": f"RATE.{code}.OIS.{tenor}"}
        for tenor in DEFAULT_CURVE_TENORS
    ]
