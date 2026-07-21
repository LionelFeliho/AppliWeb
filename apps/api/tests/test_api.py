from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


CSV = """valuation_date,source,source_symbol,canonical_id,asset_class,quote_type,raw_value,conversion,factor,shift,currency,unit\n2026-07-20,ECB,ESTR,RATE.EUR.ESTR.ON,RATES,RATE,2.00,percent_to_decimal,1,0,EUR,DECIMAL\n2026-07-20,CME,SR3U6,RATE.USD.SOFR.3M.SEP2026,RATES,RATE,95.25,futures_price_to_rate,1,0,USD,DECIMAL\n"""


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        admin_api_key="test-secret",
    )
    return TestClient(app)


def test_ingestion_is_protected_and_idempotent(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        unauthorized = client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
            content=CSV,
            headers={"Content-Type": "text/csv"},
        )
        assert unauthorized.status_code == 401

        first = client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
            content=CSV,
            headers={"Content-Type": "text/csv", "X-API-Key": "test-secret"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["rows_inserted"] == 2
        assert first.json()["quote_count"] == 2

        second = client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
            content=CSV,
            headers={"Content-Type": "text/csv", "X-API-Key": "test-secret"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["rows_unchanged"] == 2

        snapshot = client.get("/api/v1/market-data/snapshots/2026-07-20")
        assert snapshot.status_code == 200
        assert len(snapshot.json()["checksum"]) == 64

        quotes = client.get("/api/v1/market-data/snapshots/2026-07-20/quotes")
        assert quotes.status_code == 200
        values = {item["canonical_id"]: item["normalized_value"] for item in quotes.json()}
        assert values["RATE.EUR.ESTR.ON"] == "0.020000000000"
        assert values["RATE.USD.SOFR.3M.SEP2026"] == "0.047500000000"


def test_changed_quote_requires_replace(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        headers = {"Content-Type": "text/csv", "X-API-Key": "test-secret"}
        assert client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
            content=CSV,
            headers=headers,
        ).status_code == 200

        changed = CSV.replace("2.00,percent", "2.10,percent")
        conflict = client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
            content=changed,
            headers=headers,
        )
        assert conflict.status_code == 409

        replaced = client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20&replace=true",
            content=changed,
            headers=headers,
        )
        assert replaced.status_code == 200
        assert replaced.json()["rows_updated"] == 1


def test_zero_coupon_pricer_consumes_dated_snapshot(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        headers = {"Content-Type": "text/csv", "X-API-Key": "test-secret"}
        client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
            content=CSV,
            headers=headers,
        )
        response = client.post(
            "/api/v1/pricing/zero-coupon",
            json={
                "valuation_date": "2026-07-20",
                "maturity_date": "2027-07-20",
                "notional": "1000000",
                "currency": "EUR",
                "discount_rate_quote_id": "RATE.EUR.ESTR.ON",
                "compounding": "continuous",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["discount_rate"] == "0.020000000000"
        assert 980000 < float(payload["present_value"]) < 981000
        assert len(payload["snapshot_checksum"]) == 64


def test_checksum_captures_quote_metadata_changes(tmp_path: Path) -> None:
    csv_with_book = (
        "valuation_date,source,source_symbol,canonical_id,asset_class,quote_type,"
        "raw_value,conversion,factor,shift,currency,unit,book\n"
        "2026-07-20,CME,SR3U6,RATE.USD.SOFR.3M.SEP2026,RATES,RATE,"
        "95.25,futures_price_to_rate,1,0,USD,DECIMAL,USD-RATES\n"
    )
    with make_client(tmp_path) as client:
        headers = {"Content-Type": "text/csv", "X-API-Key": "test-secret"}
        first = client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
            content=csv_with_book,
            headers=headers,
        )
        assert first.status_code == 200, first.text
        first_checksum = first.json()["checksum"]

        changed_metadata = csv_with_book.replace("USD-RATES", "USD-RATES-ALT")
        replaced = client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20&replace=true",
            content=changed_metadata,
            headers=headers,
        )
        assert replaced.status_code == 200, replaced.text
        assert replaced.json()["checksum"] != first_checksum


def test_zero_coupon_rejects_rate_currency_mismatch(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        headers = {"Content-Type": "text/csv", "X-API-Key": "test-secret"}
        client.post(
            "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
            content=CSV,
            headers=headers,
        )
        response = client.post(
            "/api/v1/pricing/zero-coupon",
            json={
                "valuation_date": "2026-07-20",
                "maturity_date": "2027-07-20",
                "notional": "1000000",
                "currency": "GBP",
                "discount_rate_quote_id": "RATE.EUR.ESTR.ON",
                "compounding": "continuous",
            },
        )
        assert response.status_code == 400
        assert "does not match" in response.json()["detail"]


def _csv_for(valuation_date: str, rate_percent: str = "2.00") -> str:
    return (
        "valuation_date,source,source_symbol,canonical_id,asset_class,quote_type,"
        "raw_value,conversion,factor,shift,currency,unit\n"
        f"{valuation_date},ECB,ESTR,RATE.EUR.ESTR.ON,RATES,RATE,"
        f"{rate_percent},percent_to_decimal,1,0,EUR,DECIMAL\n"
    )


def _ingest(client: TestClient, valuation_date: str, rate_percent: str = "2.00"):
    return client.post(
        f"/api/v1/market-data/ingestions/settlements?valuation_date={valuation_date}",
        content=_csv_for(valuation_date, rate_percent),
        headers={"Content-Type": "text/csv", "X-API-Key": "test-secret"},
    )


def test_as_of_exact_and_previous_ready_resolution(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert _ingest(client, "2026-07-17").status_code == 200

        exact_missing = client.get(
            "/api/v1/market-data/as-of/2026-07-18?policy=exact"
        )
        assert exact_missing.status_code == 404

        previous = client.get(
            "/api/v1/market-data/as-of/2026-07-18?policy=previous_ready"
        )
        assert previous.status_code == 200, previous.text
        payload = previous.json()
        assert payload["as_of_date"] == "2026-07-18"
        assert payload["market_data_date"] == "2026-07-17"
        assert payload["snapshot_policy"] == "previous_ready"
        assert payload["exact_snapshot"] is False
        assert payload["snapshot"]["valuation_date"] == "2026-07-17"


def test_previous_ready_never_looks_forward(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert _ingest(client, "2026-07-17").status_code == 200
        response = client.get(
            "/api/v1/market-data/as-of/2026-07-16?policy=previous_ready"
        )
        assert response.status_code == 404
        assert "on or before" in response.json()["detail"]


def test_as_of_quotes_and_snapshot_inventory(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert _ingest(client, "2026-07-17", "2.00").status_code == 200
        assert _ingest(client, "2026-07-20", "2.10").status_code == 200

        snapshots = client.get("/api/v1/market-data/snapshots?limit=100")
        assert snapshots.status_code == 200
        assert [row["valuation_date"] for row in snapshots.json()] == [
            "2026-07-20",
            "2026-07-17",
        ]

        quotes = client.get(
            "/api/v1/market-data/as-of/2026-07-19/quotes?policy=previous_ready"
        )
        assert quotes.status_code == 200, quotes.text
        payload = quotes.json()
        assert payload["market_data_date"] == "2026-07-17"
        assert payload["quotes"][0]["normalized_value"] == "0.020000000000"


def test_zero_coupon_can_use_previous_ready_market_data(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert _ingest(client, "2026-07-17", "2.00").status_code == 200
        response = client.post(
            "/api/v1/pricing/zero-coupon",
            json={
                "as_of_date": "2026-07-19",
                "maturity_date": "2027-07-19",
                "notional": "1000000",
                "currency": "EUR",
                "discount_rate_quote_id": "RATE.EUR.ESTR.ON",
                "compounding": "continuous",
                "snapshot_policy": "previous_ready",
            },
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["as_of_date"] == "2026-07-19"
        assert payload["market_data_date"] == "2026-07-17"
        assert payload["exact_snapshot"] is False
        assert payload["discount_rate"] == "0.020000000000"
        assert len(payload["snapshot_checksum"]) == 64
