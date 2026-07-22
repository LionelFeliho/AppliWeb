from datetime import date

import pytest

from app.quant.conventions import (
    adjust_business_day,
    generate_schedule,
    parse_tenor_years,
    year_fraction,
)
from app.quant.curves import CurveNode, ZeroCurve
from app.quant.sensitivities import (
    calculate_clean_pv_sensitivities,
    calculate_xva_spread_sensitivities,
)
from app.quant.xccy import (
    SimulationAssumptions,
    XvaAssumptions,
    build_trade,
    calculate_xva,
    cross_gamma_diagnostics,
    price_trade,
    simulate_exposure_profile,
)


def curve(currency: str, rates: list[float]) -> ZeroCurve:
    tenors = [("3M", 0.25), ("1Y", 1.0), ("2Y", 2.0), ("5Y", 5.0)]
    return ZeroCurve(
        currency,
        [
            CurveNode(tenor, time, rate, f"RATE.{currency}.OIS.{tenor}")
            for (tenor, time), rate in zip(tenors, rates)
        ],
    )


def trade():
    return build_trade(
        as_of_date=date(2026, 7, 20),
        maturity_date=date(2028, 7, 20),
        base_currency="EUR",
        quote_currency="USD",
        notional_base=10_000_000,
        notional_quote=11_000_000,
        pay_base=True,
        base_spread=0.0,
        quote_spread=0.001,
        payment_frequency_months=3,
        base_day_count="ACT/360",
        quote_day_count="ACT/360",
        exchange_notionals=True,
    )


def test_conventions_and_schedule() -> None:
    assert parse_tenor_years("6M") == pytest.approx(0.5)
    assert year_fraction(date(2026, 1, 1), date(2026, 7, 1), "ACT/360") == pytest.approx(
        181 / 360
    )
    assert adjust_business_day(date(2026, 7, 18), "following") == date(2026, 7, 20)
    schedule = generate_schedule(date(2026, 7, 20), date(2027, 7, 20), 3)
    assert len(schedule) == 4
    assert schedule[-1] == date(2027, 7, 20)


def test_zero_curve_discount_and_forward() -> None:
    eur = curve("EUR", [0.02, 0.021, 0.022, 0.025])
    assert eur.discount(0.0) == 1.0
    assert eur.discount(2.0) == pytest.approx(__import__("math").exp(-0.022 * 2.0))
    assert eur.forward_rate(1.0, 2.0, 1.0) > 0
    bumped = eur.parallel_bump(0.0001)
    assert bumped.discount(2.0) < eur.discount(2.0)
    bucket_bumped = eur.node_bump(2, 0.0001)
    assert bucket_bumped.nodes[2].zero_rate == pytest.approx(eur.nodes[2].zero_rate + 0.0001)
    assert bucket_bumped.nodes[1].zero_rate == eur.nodes[1].zero_rate


def test_discounting_modes_match_without_basis() -> None:
    eur = curve("EUR", [0.02, 0.021, 0.022, 0.025])
    usd = curve("USD", [0.04, 0.041, 0.042, 0.043])
    values = [
        price_trade(trade(), eur, usd, 1.1, 0.0, mode, "USD")
        for mode in ("native", "base_collateral", "quote_collateral")
    ]
    assert max(values) - min(values) < 1e-6


def test_basis_creates_discount_switch_and_cross_gamma() -> None:
    eur = curve("EUR", [0.02, 0.021, 0.022, 0.025])
    usd = curve("USD", [0.04, 0.041, 0.042, 0.043])
    native = price_trade(trade(), eur, usd, 1.1, 0.0025, "native", "USD")
    base_collateral = price_trade(
        trade(), eur, usd, 1.1, 0.0025, "base_collateral", "USD"
    )
    assert abs(base_collateral - native) > 1.0
    gamma = cross_gamma_diagnostics(
        trade(), eur, usd, 1.1, 0.0025, "base_collateral", "USD"
    )
    assert gamma["fx_base_discount_cross_gamma"] != 0


def test_exposure_and_xva_are_reproducible() -> None:
    eur = curve("EUR", [0.02, 0.021, 0.022, 0.025])
    usd = curve("USD", [0.04, 0.041, 0.042, 0.043])
    assumptions = SimulationAssumptions(
        paths=64,
        seed=7,
        fx_volatility=0.10,
        base_rate_volatility=0.005,
        quote_rate_volatility=0.005,
        fx_base_rate_correlation=-0.2,
        fx_quote_rate_correlation=0.2,
        base_quote_rate_correlation=0.5,
        pfe_quantile=0.95,
        collateral_threshold=100_000,
        minimum_transfer_amount=10_000,
    )
    profile_a = simulate_exposure_profile(
        trade(), eur, usd, 1.1, 0.001, "quote_collateral", "USD", assumptions
    )
    profile_b = simulate_exposure_profile(
        trade(), eur, usd, 1.1, 0.001, "quote_collateral", "USD", assumptions
    )
    assert profile_a == profile_b
    assert profile_a[0]["epe"] >= 0
    assert profile_a[-1]["epe"] == 0
    clean_pv = price_trade(trade(), eur, usd, 1.1, 0.001, "quote_collateral", "USD")
    metrics = calculate_xva(
        clean_pv,
        profile_a,
        usd,
        XvaAssumptions(
            counterparty_spread=0.01,
            own_spread=0.008,
            counterparty_recovery=0.4,
            own_recovery=0.4,
            funding_spread=0.005,
            collateral_spread=0.0005,
        ),
    )
    assert metrics["cva"] >= 0
    assert metrics["dva"] >= 0
    assert metrics["xva_adjusted_pv"] == pytest.approx(clean_pv + metrics["total_xva"])


def test_clean_pv_and_xva_sensitivity_term_structures() -> None:
    eur = curve("EUR", [0.02, 0.021, 0.022, 0.025])
    usd = curve("USD", [0.04, 0.041, 0.042, 0.043])
    current_trade = trade()
    clean = calculate_clean_pv_sensitivities(
        trade=current_trade,
        base_curve=eur,
        quote_curve=usd,
        spot=1.1,
        basis=0.001,
        mode="quote_collateral",
        reporting_currency="USD",
        rate_bump_bps=1.0,
        fx_bump_relative=0.01,
        basis_bump_bps=1.0,
        spread_bump_bps=1.0,
    )
    assert [point["tenor"] for point in clean["base_curve"]] == ["3M", "1Y", "2Y", "5Y"]
    assert len(clean["quote_curve"]) == 4
    assert clean["summary"]["fx_delta_1pct"] != 0
    assert clean["summary"]["cross_currency_basis_pv01"] != 0
    assert clean["summary"]["base_curve_parallel_pv01"] == pytest.approx(
        sum(point["pv01"] for point in clean["base_curve"]),
        rel=5e-3,
        abs=5e-2,
    )

    assumptions = SimulationAssumptions(
        paths=32,
        seed=11,
        fx_volatility=0.10,
        base_rate_volatility=0.005,
        quote_rate_volatility=0.005,
        fx_base_rate_correlation=-0.2,
        fx_quote_rate_correlation=0.2,
        base_quote_rate_correlation=0.5,
        pfe_quantile=0.95,
        collateral_threshold=100_000,
        minimum_transfer_amount=10_000,
    )
    profile = simulate_exposure_profile(
        current_trade, eur, usd, 1.1, 0.001, "quote_collateral", "USD", assumptions
    )
    clean_pv = price_trade(
        current_trade, eur, usd, 1.1, 0.001, "quote_collateral", "USD"
    )
    xva_sensitivities = calculate_xva_spread_sensitivities(
        clean_pv=clean_pv,
        profile=profile,
        reporting_curve=usd,
        assumptions=XvaAssumptions(
            counterparty_spread=0.01,
            own_spread=0.008,
            counterparty_recovery=0.4,
            own_recovery=0.4,
            funding_spread=0.005,
            collateral_spread=0.0005,
        ),
        spread_bump_bps=1.0,
    )
    assert xva_sensitivities["counterparty_cva01"] >= 0
    assert xva_sensitivities["counterparty_total_xva01"] <= 0
    assert xva_sensitivities["own_dva01"] >= 0
    assert xva_sensitivities["own_total_xva01"] >= 0
    assert xva_sensitivities["funding_total_xva01"] <= 0
