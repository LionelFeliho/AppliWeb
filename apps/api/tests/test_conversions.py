from decimal import Decimal

import pytest

from app.market_data.conversions import normalize_value
from app.market_data.domain import ConversionKind, MarketDataValidationError


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        (ConversionKind.IDENTITY, "1.1426", "1.1426"),
        (ConversionKind.PERCENT_TO_DECIMAL, "4.38", "0.0438"),
        (ConversionKind.BPS_TO_DECIMAL, "85", "0.0085"),
        (ConversionKind.FUTURES_PRICE_TO_RATE, "95.25", "0.0475"),
        (ConversionKind.INVERSE, "2", "0.5"),
        (ConversionKind.FX_PIPS, "123", "0.0123"),
        (ConversionKind.CENTS_TO_UNIT, "548.25", "5.4825"),
    ],
)
def test_normalization_rules(kind: ConversionKind, raw: str, expected: str) -> None:
    assert normalize_value(Decimal(raw), kind) == Decimal(expected)


def test_factor_and_shift_are_applied_after_base_conversion() -> None:
    value = normalize_value(
        Decimal("4.5"),
        ConversionKind.PERCENT_TO_DECIMAL,
        factor=Decimal("2"),
        shift=Decimal("0.001"),
    )
    assert value == Decimal("0.091")


def test_inverse_zero_is_rejected() -> None:
    with pytest.raises(MarketDataValidationError):
        normalize_value(Decimal("0"), ConversionKind.INVERSE)
