from math import isclose

from app.quant.credit_cross_section import (
    BasketConstituent,
    CreditObservation,
    build_basket_credit_curve,
    calculate_credit_xva,
    fit_cross_section_curve,
)
from app.quant.conventions import parse_tenor_years


def observations() -> list[CreditObservation]:
    global_spread = 0.009
    sectors = {"FINANCIALS": 1.4, "INDUSTRIALS": 0.8}
    regions = {"EUROPE": 1.1, "NORTH_AMERICA": 0.9}
    ratings = {"A": 0.6, "BBB": 1.0, "BB": 1.8}
    seniorities = {"SENIOR": 1.0, "SUBORDINATED": 1.25}
    result: list[CreditObservation] = []
    index = 0
    for sector, sector_factor in sectors.items():
        for region, region_factor in regions.items():
            for rating, rating_factor in ratings.items():
                for seniority, seniority_factor in seniorities.items():
                    index += 1
                    result.append(
                        CreditObservation(
                            entity_id=f"E{index}",
                            tenor="5Y",
                            time=parse_tenor_years("5Y"),
                            spread=(
                                global_spread
                                * sector_factor
                                * region_factor
                                * rating_factor
                                * seniority_factor
                            ),
                            sector=sector,
                            region=region,
                            rating=rating,
                            seniority=seniority,
                            weight=float(3 + index % 4),
                            quote_id=f"CR.CDS.E{index}.5Y",
                        )
                    )
    return result


def test_nomura_log_regression_reconstructs_cross_section() -> None:
    fit = fit_cross_section_curve(
        observations(), minimum_observations=8, ridge=0.0
    )[0]
    predicted, warnings = fit.proxy_spread(
        {
            "sector": "FINANCIALS",
            "region": "EUROPE",
            "rating": "BBB",
            "seniority": "SENIOR",
        }
    )
    assert warnings == []
    assert isclose(predicted, 0.009 * 1.4 * 1.1, rel_tol=1e-10)
    assert fit.log_r_squared > 0.999999999
    assert fit.observations == 24


def test_basket_curve_survival_is_monotone() -> None:
    base = observations()
    all_observations: list[CreditObservation] = []
    for tenor, multiplier in (
        ("1Y", 0.6),
        ("3Y", 0.8),
        ("5Y", 1.0),
        ("10Y", 1.2),
    ):
        for observation in base:
            all_observations.append(
                CreditObservation(
                    **{
                        **observation.__dict__,
                        "tenor": tenor,
                        "time": parse_tenor_years(tenor),
                        "spread": observation.spread * multiplier,
                        "quote_id": observation.quote_id.replace("5Y", tenor),
                    }
                )
            )
    fits = fit_cross_section_curve(all_observations, minimum_observations=8)
    curve, payload = build_basket_credit_curve(
        fits,
        [
            BasketConstituent(
                "FINANCIALS", "EUROPE", "BBB", "SENIOR", 0.7
            ),
            BasketConstituent(
                "INDUSTRIALS", "NORTH_AMERICA", "A", "SENIOR", 0.3
            ),
        ],
        recovery_rate=0.4,
    )
    survivals = [node.survival_probability for node in curve.nodes]
    assert survivals == sorted(survivals, reverse=True)
    assert all(node.hazard_rate > 0 for node in curve.nodes)
    assert len(payload) == 4


def test_credit_curve_can_drive_cva_and_dva() -> None:
    fit = fit_cross_section_curve(observations(), minimum_observations=8)
    cp_curve, _ = build_basket_credit_curve(
        fit,
        [BasketConstituent("FINANCIALS", "EUROPE", "BB", "SENIOR")],
        recovery_rate=0.4,
    )
    own_curve, _ = build_basket_credit_curve(
        fit,
        [
            BasketConstituent(
                "INDUSTRIALS", "NORTH_AMERICA", "A", "SENIOR"
            )
        ],
        recovery_rate=0.4,
    )
    metrics = calculate_credit_xva(
        [
            {"time": 0.0, "epe": 100_000.0, "ene": -50_000.0},
            {"time": 2.5, "epe": 160_000.0, "ene": -70_000.0},
            {"time": 5.0, "epe": 0.0, "ene": 0.0},
        ],
        cp_curve,
        own_curve=own_curve,
        discount_rate=0.02,
    )
    assert metrics["cva"] > 0.0
    assert metrics["dva"] > 0.0
    assert metrics["net_credit_xva"] == -metrics["cva"] + metrics["dva"]
