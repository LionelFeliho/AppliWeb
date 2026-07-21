from __future__ import annotations

import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import httpx

from .domain import (
    AssetClass,
    ConversionKind,
    EcbSeriesSpec,
    MarketDataValidationError,
    ProviderError,
    QuoteType,
    RawQuote,
)


class CsvSettlementProvider:
    name = "settlement_csv"

    _known_columns = {
        "valuation_date",
        "source",
        "source_symbol",
        "symbol",
        "canonical_id",
        "asset_class",
        "quote_type",
        "raw_value",
        "settlement_price",
        "settle",
        "conversion",
        "factor",
        "shift",
        "currency",
        "unit",
        "metadata_json",
    }

    def parse_file(self, path: str | Path, valuation_date: date) -> list[RawQuote]:
        file_path = Path(path)
        try:
            text = file_path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ProviderError(f"Unable to read settlement file {file_path}: {exc}") from exc
        return self.parse_text(text, valuation_date)

    def parse_text(self, text: str, valuation_date: date) -> list[RawQuote]:
        if not text.strip():
            raise MarketDataValidationError("Settlement CSV is empty")

        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        if not reader.fieldnames:
            raise MarketDataValidationError("Settlement CSV has no header")

        normalized_headers = [header.strip().lower() for header in reader.fieldnames]
        if "canonical_id" not in normalized_headers:
            raise MarketDataValidationError("Missing required column: canonical_id")
        if not {"raw_value", "settlement_price", "settle"}.intersection(normalized_headers):
            raise MarketDataValidationError(
                "Missing quote value column: raw_value, settlement_price or settle"
            )

        quotes: list[RawQuote] = []
        errors: list[str] = []

        for line_number, original_row in enumerate(reader, start=2):
            row = {
                (key or "").strip().lower(): (value or "").strip()
                for key, value in original_row.items()
            }
            if not any(row.values()):
                continue
            try:
                quotes.append(self._parse_row(row, valuation_date))
            except (ValueError, InvalidOperation, json.JSONDecodeError) as exc:
                errors.append(f"line {line_number}: {exc}")
            except MarketDataValidationError as exc:
                errors.extend(f"line {line_number}: {message}" for message in exc.errors)

        if errors:
            raise MarketDataValidationError(errors)
        if not quotes:
            raise MarketDataValidationError("Settlement CSV contains no quote rows")
        return quotes

    def _parse_row(self, row: dict[str, str], requested_date: date) -> RawQuote:
        row_date = date.fromisoformat(row["valuation_date"]) if row.get("valuation_date") else requested_date
        if row_date != requested_date:
            raise MarketDataValidationError(
                f"row valuation_date {row_date} does not match requested date {requested_date}"
            )

        raw_text = row.get("raw_value") or row.get("settlement_price") or row.get("settle")
        if raw_text is None or raw_text == "":
            raise MarketDataValidationError("raw quote value is empty")

        canonical_id = row.get("canonical_id", "").strip()
        if not canonical_id:
            raise MarketDataValidationError("canonical_id is empty")

        asset_class = AssetClass(row.get("asset_class", "").upper())
        quote_type = QuoteType(row.get("quote_type", "").upper())
        conversion = ConversionKind((row.get("conversion") or "identity").lower())
        factor = Decimal(row.get("factor") or "1")
        shift = Decimal(row.get("shift") or "0")
        raw_value = Decimal(raw_text.replace(" ", "").replace(",", ""))

        metadata: dict[str, object] = {}
        if row.get("metadata_json"):
            decoded = json.loads(row["metadata_json"])
            if not isinstance(decoded, dict):
                raise MarketDataValidationError("metadata_json must contain a JSON object")
            metadata.update(decoded)

        extras = {
            key: value
            for key, value in row.items()
            if key not in self._known_columns and value != ""
        }
        if extras:
            metadata["source_columns"] = extras

        source = row.get("source") or self.name
        source_symbol = row.get("source_symbol") or row.get("symbol") or canonical_id
        currency = (row.get("currency") or "").upper() or None
        unit = (row.get("unit") or "DECIMAL").upper()

        return RawQuote(
            valuation_date=row_date,
            canonical_id=canonical_id,
            asset_class=asset_class,
            quote_type=quote_type,
            raw_value=raw_value,
            source=source,
            source_symbol=source_symbol,
            conversion=conversion,
            factor=factor,
            shift=shift,
            currency=currency,
            unit=unit,
            metadata=metadata,
        )


DEFAULT_ECB_SERIES: tuple[EcbSeriesSpec, ...] = (
    EcbSeriesSpec(
        flow="EXR",
        key="D.USD.EUR.SP00.A",
        canonical_id="FX.EURUSD.SPOT",
        asset_class=AssetClass.FX,
        quote_type=QuoteType.SPOT,
        currency="USD",
        unit="USD_PER_EUR",
    ),
    EcbSeriesSpec(
        flow="EXR",
        key="D.GBP.EUR.SP00.A",
        canonical_id="FX.EURGBP.SPOT",
        asset_class=AssetClass.FX,
        quote_type=QuoteType.SPOT,
        currency="GBP",
        unit="GBP_PER_EUR",
    ),
    EcbSeriesSpec(
        flow="EXR",
        key="D.JPY.EUR.SP00.A",
        canonical_id="FX.EURJPY.SPOT",
        asset_class=AssetClass.FX,
        quote_type=QuoteType.SPOT,
        currency="JPY",
        unit="JPY_PER_EUR",
    ),
    EcbSeriesSpec(
        flow="EXR",
        key="D.CHF.EUR.SP00.A",
        canonical_id="FX.EURCHF.SPOT",
        asset_class=AssetClass.FX,
        quote_type=QuoteType.SPOT,
        currency="CHF",
        unit="CHF_PER_EUR",
    ),
    EcbSeriesSpec(
        flow="EST",
        key="B.EU000A2X2A25.WT",
        canonical_id="RATE.EUR.ESTR.ON",
        asset_class=AssetClass.RATES,
        quote_type=QuoteType.RATE,
        currency="EUR",
        unit="DECIMAL",
        conversion=ConversionKind.PERCENT_TO_DECIMAL,
    ),
)


class EcbSdmxProvider:
    name = "ECB"

    def __init__(
        self,
        base_url: str = "https://data-api.ecb.europa.eu/service",
        timeout_seconds: int = 30,
        lookback_days: int = 7,
        max_staleness_days: int = 7,
        series: Iterable[EcbSeriesSpec] = DEFAULT_ECB_SERIES,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.lookback_days = lookback_days
        self.max_staleness_days = max_staleness_days
        self.series = tuple(series)
        self._client = client

    def fetch(self, valuation_date: date) -> list[RawQuote]:
        own_client = self._client is None
        client = self._client or httpx.Client(
            timeout=self.timeout_seconds,
            headers={"Accept": "text/csv", "User-Agent": "xva-eod-market-data/0.1"},
            follow_redirects=True,
        )
        try:
            return [self._fetch_series(client, spec, valuation_date) for spec in self.series]
        finally:
            if own_client:
                client.close()

    def _fetch_series(
        self, client: httpx.Client, spec: EcbSeriesSpec, valuation_date: date
    ) -> RawQuote:
        start = valuation_date - timedelta(days=self.lookback_days)
        url = f"{self.base_url}/data/{spec.flow}/{spec.key}"
        params = {
            "startPeriod": start.isoformat(),
            "endPeriod": valuation_date.isoformat(),
            "format": "csvdata",
            "detail": "dataonly",
        }
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"ECB request failed for {spec.flow}/{spec.key}: {exc}") from exc

        observations = self._parse_observations(response.text)
        eligible = [item for item in observations if item[0] <= valuation_date]
        if not eligible:
            raise ProviderError(
                f"ECB returned no observation on or before {valuation_date} for {spec.flow}/{spec.key}"
            )

        observed_date, raw_value = max(eligible, key=lambda item: item[0])
        staleness_days = (valuation_date - observed_date).days
        if staleness_days > self.max_staleness_days:
            raise ProviderError(
                f"ECB observation for {spec.flow}/{spec.key} is {staleness_days} days stale "
                f"(maximum {self.max_staleness_days})"
            )

        return RawQuote(
            valuation_date=valuation_date,
            canonical_id=spec.canonical_id,
            asset_class=spec.asset_class,
            quote_type=spec.quote_type,
            raw_value=raw_value,
            source=self.name,
            source_symbol=f"{spec.flow}.{spec.key}",
            conversion=spec.conversion,
            factor=spec.factor,
            shift=spec.shift,
            currency=spec.currency,
            unit=spec.unit,
            metadata={
                "source_observation_date": observed_date.isoformat(),
                "staleness_days": staleness_days,
                "ecb_flow": spec.flow,
                "ecb_key": spec.key,
            },
        )

    @staticmethod
    def _parse_observations(text: str) -> list[tuple[date, Decimal]]:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        if not reader.fieldnames:
            raise ProviderError("ECB CSV response has no header")
        observations: list[tuple[date, Decimal]] = []
        for original_row in reader:
            row = {
                (key or "").strip().upper(): (value or "").strip()
                for key, value in original_row.items()
            }
            period = row.get("TIME_PERIOD")
            value = row.get("OBS_VALUE")
            if not period or value in {None, ""}:
                continue
            try:
                observations.append((date.fromisoformat(period[:10]), Decimal(value)))
            except (ValueError, InvalidOperation) as exc:
                raise ProviderError(
                    f"Invalid ECB observation TIME_PERIOD={period!r}, OBS_VALUE={value!r}"
                ) from exc
        return observations
