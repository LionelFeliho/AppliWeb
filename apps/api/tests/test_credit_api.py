from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


ROOT = Path(__file__).resolve().parents[3]
DEMO = (ROOT / "sample_data" / "credit_cross_section_demo.csv").read_text(
    encoding="utf-8"
)


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            database_url=f"sqlite:///{tmp_path / 'credit.db'}",
            admin_api_key="test-secret",
        )
    )


def ingest(client: TestClient) -> None:
    response = client.post(
        "/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20",
        content=DEMO,
        headers={"Content-Type": "text/csv", "X-API-Key": "test-secret"},
    )
    assert response.status_code == 200, response.text


def settings() -> dict:
    return {
        "currency": "USD",
        "tenors": ["1Y", "3Y", "5Y", "7Y", "10Y"],
        "minimum_observations_per_tenor": 12,
        "minimum_contributors": 3,
        "source": "DEMO_SYNTHETIC",
    }


def basket() -> list[dict]:
    return [
        {
            "name": "European BBB financial senior",
            "sector": "FINANCIALS",
            "region": "EUROPE",
            "rating": "BBB",
            "seniority": "SENIOR",
            "weight": 0.75,
        },
        {
            "name": "North American A industrial senior",
            "sector": "INDUSTRIALS",
            "region": "NORTH_AMERICA",
            "rating": "A",
            "seniority": "SENIOR",
            "weight": 0.25,
        },
    ]


def test_calibration_and_basket_curve_endpoints(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest(client)
        calibration = client.post(
            "/api/v1/credit/nomura-cross-section/calibrate",
            json={
                "as_of_date": "2026-07-20",
                "settings": settings(),
            },
        )
        assert calibration.status_code == 200, calibration.text
        payload = calibration.json()
        assert len(payload["tenor_fits"]) == 5
        assert payload["observations_used"] == 60
        assert payload["tenor_fits"][2]["diagnostics"]["log_r_squared"] > 0.99

        curve = client.post(
            "/api/v1/credit/nomura-cross-section/basket-curve",
            json={
                "as_of_date": "2026-07-20",
                "settings": settings(),
                "basket": basket(),
                "recovery_rate": 0.4,
                "aggregation": "arithmetic",
            },
        )
        assert curve.status_code == 200, curve.text
        curve_payload = curve.json()["curve"]
        assert curve_payload["curve_id"].startswith("CR.XS.NOMURA.")
        assert len(curve_payload["nodes"]) == 5
        survivals = [
            node["survival_probability"] for node in curve_payload["nodes"]
        ]
        assert survivals == sorted(survivals, reverse=True)


def test_cross_section_curve_drives_credit_xva(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest(client)
        response = client.post(
            "/api/v1/credit/nomura-cross-section/xva",
            json={
                "as_of_date": "2026-07-20",
                "settings": settings(),
                "counterparty_basket": basket(),
                "own_basket": [
                    {
                        "sector": "INDUSTRIALS",
                        "region": "NORTH_AMERICA",
                        "rating": "A",
                        "seniority": "SENIOR",
                    }
                ],
                "counterparty_recovery_rate": 0.4,
                "own_recovery_rate": 0.4,
                "discount_rate": 0.025,
                "exposure_profile": [
                    {"time": 0.0, "epe": 100000, "ene": -30000},
                    {"time": 1.0, "epe": 140000, "ene": -40000},
                    {"time": 3.0, "epe": 120000, "ene": -50000},
                    {"time": 5.0, "epe": 80000, "ene": -30000},
                    {"time": 10.0, "epe": 0, "ene": 0},
                ],
            },
        )
        assert response.status_code == 200, response.text
        metrics = response.json()["credit_xva"]
        assert metrics["cva"] > 0
        assert metrics["dva"] > 0
        assert len(metrics["intervals"]) == 4


def test_previous_ready_and_metadata_validation(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        ingest(client)
        response = client.post(
            "/api/v1/credit/nomura-cross-section/basket-curve",
            json={
                "as_of_date": "2026-07-21",
                "snapshot_policy": "previous_ready",
                "settings": settings(),
                "basket": basket(),
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["market_data_date"] == "2026-07-20"
        assert response.json()["exact_snapshot"] is False
