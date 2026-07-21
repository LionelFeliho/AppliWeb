from __future__ import annotations

from decimal import Decimal, InvalidOperation

from .domain import ConversionKind, MarketDataValidationError, RawQuote


HUNDRED = Decimal("100")
TEN_THOUSAND = Decimal("10000")


def normalize_value(
    raw_value: Decimal,
    conversion: ConversionKind,
    factor: Decimal = Decimal("1"),
    shift: Decimal = Decimal("0"),
) -> Decimal:
    try:
        if conversion in {ConversionKind.IDENTITY, ConversionKind.LINEAR}:
            base = raw_value
        elif conversion is ConversionKind.PERCENT_TO_DECIMAL:
            base = raw_value / HUNDRED
        elif conversion is ConversionKind.BPS_TO_DECIMAL:
            base = raw_value / TEN_THOUSAND
        elif conversion is ConversionKind.FUTURES_PRICE_TO_RATE:
            base = (HUNDRED - raw_value) / HUNDRED
        elif conversion is ConversionKind.INVERSE:
            if raw_value == 0:
                raise MarketDataValidationError("Cannot invert a zero quote")
            base = Decimal("1") / raw_value
        elif conversion is ConversionKind.FX_PIPS:
            base = raw_value / TEN_THOUSAND
        elif conversion is ConversionKind.CENTS_TO_UNIT:
            base = raw_value / HUNDRED
        else:  # pragma: no cover - Enum makes this defensive branch unreachable
            raise MarketDataValidationError(f"Unsupported conversion: {conversion}")
        return base * factor + shift
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise MarketDataValidationError(
            f"Failed to normalize {raw_value} with {conversion.value}"
        ) from exc


def normalize_quote(quote: RawQuote) -> Decimal:
    return normalize_value(
        raw_value=quote.raw_value,
        conversion=quote.conversion,
        factor=quote.factor,
        shift=quote.shift,
    )


CONVERSION_DESCRIPTIONS: dict[str, str] = {
    ConversionKind.IDENTITY.value: "raw",
    ConversionKind.LINEAR.value: "raw",
    ConversionKind.PERCENT_TO_DECIMAL.value: "raw / 100",
    ConversionKind.BPS_TO_DECIMAL.value: "raw / 10,000",
    ConversionKind.FUTURES_PRICE_TO_RATE.value: "(100 - raw) / 100",
    ConversionKind.INVERSE.value: "1 / raw",
    ConversionKind.FX_PIPS.value: "raw / 10,000",
    ConversionKind.CENTS_TO_UNIT.value: "raw / 100",
}
