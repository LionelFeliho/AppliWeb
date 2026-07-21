from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


ROOT = Path(__file__).resolve().parents[3]
G10_CSV = (ROOT / "sample_data" / "g10_xccy_closing_demo.csv").read_text(
    encoding="utf-8"
)


def make_client(tmp_path: Path) -> TestClient:
    app = create_app(
        database_url=f"sqlite:///{tmp_path / 'xccy.db'}",
        admin_api_key="test-secret",
    )
    return TestClient(app)


def ingest_g10(client: TestClient, valuation_date: str = "2026-07-20") -> None:
    csv_text = G10_CSV.replace("2026-07-20", valuation_date)
    response = client.post(
        f"/api/v1/market-data/ingestions/settlements?valuation_date={valuation_date}",
        content=csv_text,
        headers={"Content-Type": "text/csv", "X-API-Key": "test-secret"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["quote_count"] == 97


def request_payload(**overrides):
    payload = {
        "as_of_date": "2026-07-20",
        "snapshot_policy": "exact",
        "start_date": "2026-07-20",
        "maturity_date": "2031-07-20",
        "base_currency": "EUR",
        "quote_currency": "USD",
        "reporting_currency": "USD",
        "base_notional": 10_000_000,
        "pay_base": True,
        "base_leg": {
            "leg_type": "floating",
            "spread_bps": 0,
            "payment_frequency_months": 3,
        },
        "quote_leg": {
            "leg_type": "floating",
            "spread_bps": 20,
            "payment_frequency_months": 3,
        },
        "exchange_initial": False,
        "exchange_final": True,
        "discounting_mode": "base_collateral",
        "simulation": {
            "paths": 100,
            "time_steps_per_year": 2,
            "seed": 7,
            "pfe_quantile": 0.95,
        },
        "csa": {
            "threshold": 100_000,
            "minimum_transfer_amount": 50_000,
            "margin_period_of_risk_days": 10,
        },
        "counterparty_credit": {"hazard_rate": 0.015, "recovery_rate": 0.4},
        "own_credit": {"hazard_rate": 0.008, "recovery_rate": 0.4},
    }
    payload.update(overrides)
    return payload


def test_g10_reference_and_eurusd_readiness(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest_g10(client)
        reference = client.get("/api/v1/pricing/reference/g10")
        assert reference.status_code == 200
        assert len(reference.json()["currencies"]) == 10

        readiness = client.get(
            "/api/v1/pricing/xccy/readiness",
            params={
                "as_of_date": "2026-07-20",
                "base_currency": "EUR",
                "quote_currency": "USD",
            },
        )
        assert readiness.status_code == 200, readiness.text
        body = readiness.json()
        assert body["ready"] is True
        assert body["spot"]["value"] == 1.1426
        assert body["base_curve"]["missing_core_tenors"] == []


def test_xccy_price_exposure_xva_and_discount_switch(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest_g10(client)
        response = client.post("/api/v1/pricing/xccy-swap", json=request_payload())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["model_version"] == "xccy-eod-mvp-0.2"
        assert body["market_data_date"] == "2026-07-20"
        assert len(body["discounting_comparison"]) == 3
        assert len(body["exposure_profile"]) >= 10
        assert all(point["epe"] >= 0 for point in body["exposure_profile"])
        assert all(point["ene"] <= 0 for point in body["exposure_profile"])
        assert body["exposure_profile"][-1]["forward_mtm"] == 0
        assert body["xva"]["cva"] >= 0
        assert body["xva"]["dva"] >= 0
        assert body["pv_with_xva"] == pytest.approx(
            body["pv"] + body["xva"]["total_xva"]
        )
        modes = {row["mode"]: row for row in body["discounting_comparison"]}
        assert modes["native_ois"]["pv"] != modes["base_collateral"]["pv"]
        assert "cross_gamma_fx_basis_pnl_1pct_1bp" in body["greeks"]


def test_cross_pair_spot_vol_and_basis_are_triangulated(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest_g10(client)
        payload = request_payload(
            base_currency="EUR",
            quote_currency="JPY",
            reporting_currency="JPY",
        )
        response = client.post("/api/v1/pricing/xccy-swap", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["market"]["spot"]["path"] == ["EUR", "USD", "JPY"]
        assert "TRIANGULATED" in body["market"]["fx_volatility"]["source"]
        assert "TRIANGULATED" in body["market"]["xccy_basis"]["source"]


def test_xccy_previous_ready_uses_historical_close(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest_g10(client, "2026-07-17")
        payload = request_payload(
            as_of_date="2026-07-19",
            start_date="2026-07-21",
            snapshot_policy="previous_ready",
        )
        response = client.post("/api/v1/pricing/xccy-swap", json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["as_of_date"] == "2026-07-19"
        assert body["market_data_date"] == "2026-07-17"
        assert body["exact_snapshot"] is False


def test_zero_basis_discounting_representations_are_equivalent(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest_g10(client)
        payload = request_payload(
            basis_spread_bps_override=0,
            base_leg={
                "leg_type": "fixed",
                "fixed_rate": 0,
                "spread_bps": 0,
                "payment_frequency_months": 3,
            },
            quote_leg={
                "leg_type": "fixed",
                "fixed_rate": 0,
                "spread_bps": 0,
                "payment_frequency_months": 3,
            },
        )
        response = client.post("/api/v1/pricing/xccy-swap", json=payload)
        assert response.status_code == 200, response.text
        pvs = [row["pv"] for row in response.json()["discounting_comparison"]]
        assert max(pvs) - min(pvs) < 1e-6


def test_xccy_simulation_is_reproducible_for_same_seed(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest_g10(client)
        payload = request_payload()
        first = client.post("/api/v1/pricing/xccy-swap", json=payload)
        second = client.post("/api/v1/pricing/xccy-swap", json=payload)
        assert first.status_code == second.status_code == 200
        assert first.json()["exposure_profile"] == second.json()["exposure_profile"]
        assert first.json()["xva"] == second.json()["xva"]
