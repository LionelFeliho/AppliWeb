from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


TENORS = ["1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y"]


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            database_url=f"sqlite:///{tmp_path / 'xccy.db'}",
            admin_api_key="test-secret",
        )
    )


def market_csv(include_usd_10y: bool = True) -> str:
    header = (
        "valuation_date,source,source_symbol,canonical_id,asset_class,quote_type,"
        "raw_value,conversion,factor,shift,currency,unit\n"
    )
    rows = []
    eur_rates = [2.00, 2.05, 2.10, 2.15, 2.20, 2.35, 2.55]
    usd_rates = [4.40, 4.35, 4.30, 4.20, 4.10, 4.00, 3.90]
    for tenor, rate in zip(TENORS, eur_rates):
        rows.append(
            f"2026-07-20,DEMO,EUR-{tenor},RATE.EUR.OIS.{tenor},RATES,RATE,"
            f"{rate},percent_to_decimal,1,0,EUR,DECIMAL"
        )
    for tenor, rate in zip(TENORS, usd_rates):
        if tenor == "10Y" and not include_usd_10y:
            continue
        rows.append(
            f"2026-07-20,DEMO,USD-{tenor},RATE.USD.OIS.{tenor},RATES,RATE,"
            f"{rate},percent_to_decimal,1,0,USD,DECIMAL"
        )
    rows.append(
        "2026-07-20,DEMO,EURUSD,FX.EURUSD.SPOT,FX,SPOT,1.10,identity,1,0,USD,USD_PER_EUR"
    )
    return header + "\n".join(rows) + "\n"


def request_payload() -> dict:
    return {
        "as_of_date": "2026-07-20",
        "maturity_date": "2028-07-20",
        "base_currency": "EUR",
        "quote_currency": "USD",
        "reporting_currency": "USD",
        "notional_base": 10_000_000,
        "pay_base": True,
        "quote_spread_bps": 10,
        "payment_frequency_months": 3,
        "cross_currency_basis_bps": 25,
        "discounting_mode": "base_collateral",
        "compare_discounting": True,
        "simulation": {
            "paths": 64,
            "seed": 42,
            "fx_volatility": 0.10,
            "base_rate_volatility": 0.005,
            "quote_rate_volatility": 0.005,
            "collateral_threshold": 100000,
            "minimum_transfer_amount": 10000,
        },
        "xva": {
            "counterparty_spread_bps": 100,
            "own_spread_bps": 80,
            "funding_spread_bps": 50,
            "collateral_spread_bps": 5,
        },
    }


def ingest(client: TestClient, csv_text: str):
    return client.post(
        "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
        content=csv_text,
        headers={"Content-Type": "text/csv", "X-API-Key": "test-secret"},
    )


def test_g10_reference_endpoint(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        response = client.get("/api/v1/reference/g10")
        assert response.status_code == 200
        currencies = {row["currency"] for row in response.json()["currencies"]}
        assert currencies == {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "NOK", "SEK"}


def test_xccy_api_returns_discount_comparison_exposure_and_xva(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        imported = ingest(client, market_csv())
        assert imported.status_code == 200, imported.text
        response = client.post("/api/v1/pricing/xccy-swap", json=request_payload())
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["market_data_date"] == "2026-07-20"
        assert payload["fx_spot"] == 1.10
        assert set(payload["pv_by_discounting"]) == {
            "native",
            "base_collateral",
            "quote_collateral",
        }
        assert len(payload["exposure_profile"]) == 9
        assert payload["exposure_profile"][-1]["epe"] == 0
        assert payload["xva"]["cva"] >= 0
        assert payload["xva"]["fva"] >= 0
        assert len(payload["snapshot_checksum"]) == 64
        assert payload["discount_switch_impact"]["base_collateral"] == 0
        assert payload["cross_gamma"]["fx_base_discount_cross_gamma"] != 0


def test_strict_curve_completeness_rejects_missing_tenor(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert ingest(client, market_csv(include_usd_10y=False)).status_code == 200
        response = client.post("/api/v1/pricing/xccy-swap", json=request_payload())
        assert response.status_code == 422
        assert "Missing required tenors: 10Y" in response.json()["detail"]


def test_previous_ready_snapshot_is_supported_for_xccy(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert ingest(client, market_csv()).status_code == 200
        payload = request_payload()
        payload["as_of_date"] = "2026-07-21"
        payload["maturity_date"] = "2028-07-21"
        payload["snapshot_policy"] = "previous_ready"
        response = client.post("/api/v1/pricing/xccy-swap", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["market_data_date"] == "2026-07-20"
        assert response.json()["exact_snapshot"] is False


def test_g10_completeness_endpoint(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert ingest(client, market_csv()).status_code == 200
        response = client.get("/api/v1/market-data/completeness/g10/2026-07-20")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["complete"] is False
        assert "GBP" in payload["missing_curve_quotes"]
        assert "FX.GBPUSD.SPOT" in payload["missing_fx_quotes"]


def full_g10_demo_csv() -> str:
    return (
        Path(__file__).resolve().parents[3] / "sample_data" / "g10_xccy_demo.csv"
    ).read_text(encoding="utf-8")


def test_full_demo_snapshot_is_g10_complete_and_prices_usd_cross(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        assert ingest(client, full_g10_demo_csv()).status_code == 200
        completeness = client.get("/api/v1/market-data/completeness/g10/2026-07-20")
        assert completeness.status_code == 200
        assert completeness.json()["complete"] is True

        payload = {
            "as_of_date": "2026-07-20",
            "maturity_date": "2027-07-20",
            "base_currency": "GBP",
            "quote_currency": "JPY",
            "notional_base": 5_000_000,
            "cross_currency_basis_bps": 15,
            "discounting_mode": "quote_collateral",
            "simulation": {"paths": 32, "collateral_threshold": 50000000},
        }
        response = client.post("/api/v1/pricing/xccy-swap", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["fx_resolution"].startswith("usd_cross")
        assert set(result["fx_spot_quote_ids"]) == {
            "FX.GBPUSD.SPOT",
            "FX.USDJPY.SPOT",
        }
        assert result["quote_currency"] == "JPY"
