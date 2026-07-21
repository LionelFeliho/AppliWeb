from datetime import date

import pytest

from app.quant.conventions import (
    G10_CURRENCIES,
    adjust_business_day,
    currency_convention,
    generate_schedule,
    year_fraction,
)
from app.quant.curves import CurvePoint, DiscountCurve, tenor_to_years


def test_g10_convention_registry_and_schedule() -> None:
    assert set(G10_CURRENCIES) == {
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "CHF",
        "CAD",
        "AUD",
        "NZD",
        "SEK",
        "NOK",
    }
    assert currency_convention("gbp").day_count == "ACT/365F"
    assert adjust_business_day(date(2026, 7, 19)) == date(2026, 7, 20)
    schedule = generate_schedule(date(2026, 7, 20), date(2027, 7, 20), 3)
    assert schedule[-1] == date(2027, 7, 20)
    assert len(schedule) == 4
    assert year_fraction(date(2026, 7, 20), date(2027, 7, 20), "ACT/365F") == 1


def test_piecewise_zero_curve_interpolation() -> None:
    curve = DiscountCurve(
        date(2026, 7, 20),
        "EUR",
        [
            CurvePoint("1Y", 1.0, 0.02, "RATE.EUR.OIS.1Y", "TEST"),
            CurvePoint("5Y", 5.0, 0.03, "RATE.EUR.OIS.5Y", "TEST"),
        ],
    )
    assert tenor_to_years("6M") == 0.5
    assert curve.zero_rate(3.0) == pytest.approx(0.025)
    assert curve.discount_factor(3.0) == pytest.approx(0.9277434863)
    assert curve.forward_rate(1.0, 2.0, 1.0) > 0
