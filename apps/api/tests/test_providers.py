from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.market_data.domain import (
    AssetClass,
    ConversionKind,
    EcbSeriesSpec,
    MarketDataValidationError,
    QuoteType,
)
from app.market_data.providers import CsvSettlementProvider, EcbSdmxProvider


def test_csv_provider_parses_settlement_alias_and_metadata() -> None:
    text = """valuation_date,source,symbol,canonical_id,asset_class,quote_type,settlement_price,conversion,currency,unit,book\n2026-07-20,CME,SR3U6,RATE.USD.SOFR.3M.SEP2026,RATES,RATE,95.25,futures_price_to_rate,USD,DECIMAL,USD-RATES\n"""
    quote = CsvSettlementProvider().parse_text(text, date(2026, 7, 20))[0]
    assert quote.raw_value == Decimal("95.25")
    assert quote.source_symbol == "SR3U6"
    assert quote.conversion is ConversionKind.FUTURES_PRICE_TO_RATE
    assert quote.metadata["source_columns"]["book"] == "USD-RATES"


def test_csv_provider_rejects_mixed_valuation_dates() -> None:
    text = """valuation_date,canonical_id,asset_class,quote_type,raw_value\n2026-07-19,FX.EURUSD.SPOT,FX,SPOT,1.1\n"""
    with pytest.raises(MarketDataValidationError):
        CsvSettlementProvider().parse_text(text, date(2026, 7, 20))


def test_ecb_provider_chooses_latest_observation_before_eod() -> None:
    csv_body = """KEY,TIME_PERIOD,OBS_VALUE\nEXR.D.USD.EUR.SP00.A,2026-07-17,1.1400\nEXR.D.USD.EUR.SP00.A,2026-07-20,1.1426\n"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "csvdata"
        return httpx.Response(200, text=csv_body, headers={"Content-Type": "text/csv"})

    spec = EcbSeriesSpec(
        flow="EXR",
        key="D.USD.EUR.SP00.A",
        canonical_id="FX.EURUSD.SPOT",
        asset_class=AssetClass.FX,
        quote_type=QuoteType.SPOT,
        currency="USD",
        unit="USD_PER_EUR",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = EcbSdmxProvider(series=[spec], client=client)
    quote = provider.fetch(date(2026, 7, 20))[0]
    assert quote.raw_value == Decimal("1.1426")
    assert quote.metadata["source_observation_date"] == "2026-07-20"
    assert quote.metadata["staleness_days"] == 0
    client.close()
