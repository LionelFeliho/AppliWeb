from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date
from typing import Literal

from .conventions import DayCount, generate_schedule, year_fraction
from .curves import ZeroCurve


DiscountingMode = Literal["native", "base_collateral", "quote_collateral"]


@dataclass(frozen=True)
class Period:
    start_date: date
    end_date: date
    start_time: float
    end_time: float
    base_accrual: float
    quote_accrual: float


@dataclass(frozen=True)
class XccyTrade:
    as_of_date: date
    maturity_date: date
    base_currency: str
    quote_currency: str
    notional_base: float
    notional_quote: float
    pay_base: bool
    base_spread: float
    quote_spread: float
    exchange_notionals: bool
    periods: tuple[Period, ...]


@dataclass(frozen=True)
class SimulationAssumptions:
    paths: int
    seed: int
    fx_volatility: float
    base_rate_volatility: float
    quote_rate_volatility: float
    fx_base_rate_correlation: float
    fx_quote_rate_correlation: float
    base_quote_rate_correlation: float
    pfe_quantile: float
    collateral_threshold: float
    minimum_transfer_amount: float


@dataclass(frozen=True)
class XvaAssumptions:
    counterparty_spread: float
    own_spread: float
    counterparty_recovery: float
    own_recovery: float
    funding_spread: float
    collateral_spread: float


def build_trade(
    *,
    as_of_date: date,
    maturity_date: date,
    base_currency: str,
    quote_currency: str,
    notional_base: float,
    notional_quote: float,
    pay_base: bool,
    base_spread: float,
    quote_spread: float,
    payment_frequency_months: int,
    base_day_count: DayCount,
    quote_day_count: DayCount,
    exchange_notionals: bool,
) -> XccyTrade:
    schedule = generate_schedule(as_of_date, maturity_date, payment_frequency_months)
    periods: list[Period] = []
    previous = as_of_date
    for payment_date in schedule:
        periods.append(
            Period(
                start_date=previous,
                end_date=payment_date,
                start_time=(previous - as_of_date).days / 365.0,
                end_time=(payment_date - as_of_date).days / 365.0,
                base_accrual=year_fraction(previous, payment_date, base_day_count),
                quote_accrual=year_fraction(previous, payment_date, quote_day_count),
            )
        )
        previous = payment_date

    return XccyTrade(
        as_of_date=as_of_date,
        maturity_date=maturity_date,
        base_currency=base_currency.upper(),
        quote_currency=quote_currency.upper(),
        notional_base=notional_base,
        notional_quote=notional_quote,
        pay_base=pay_base,
        base_spread=base_spread,
        quote_spread=quote_spread,
        exchange_notionals=exchange_notionals,
        periods=tuple(periods),
    )


def fx_forward(
    spot_quote_per_base: float,
    base_curve: ZeroCurve,
    quote_curve: ZeroCurve,
    cross_currency_basis: float,
    maturity_time: float,
    valuation_time: float = 0.0,
) -> float:
    if spot_quote_per_base <= 0:
        raise ValueError("FX spot must be positive")
    if maturity_time < valuation_time:
        raise ValueError("FX forward maturity must be after valuation time")
    base_df = base_curve.relative_discount(valuation_time, maturity_time)
    quote_df = quote_curve.relative_discount(valuation_time, maturity_time)
    return (
        spot_quote_per_base
        * base_df
        / quote_df
        * math.exp(cross_currency_basis * (maturity_time - valuation_time))
    )


def _leg_cashflows(
    trade: XccyTrade,
    base_curve: ZeroCurve,
    quote_curve: ZeroCurve,
    valuation_time: float,
) -> list[tuple[float, float, float]]:
    """Return payment time, base amount and quote amount for remaining periods."""
    base_sign = -1.0 if trade.pay_base else 1.0
    quote_sign = -base_sign
    result: list[tuple[float, float, float]] = []
    last_time = trade.periods[-1].end_time

    for period in trade.periods:
        if period.end_time <= valuation_time + 1e-12:
            continue
        base_forward = base_curve.forward_rate(
            period.start_time, period.end_time, period.base_accrual
        )
        quote_forward = quote_curve.forward_rate(
            period.start_time, period.end_time, period.quote_accrual
        )
        base_amount = (
            base_sign
            * trade.notional_base
            * (base_forward + trade.base_spread)
            * period.base_accrual
        )
        quote_amount = (
            quote_sign
            * trade.notional_quote
            * (quote_forward + trade.quote_spread)
            * period.quote_accrual
        )
        if trade.exchange_notionals and abs(period.end_time - last_time) < 1e-12:
            base_amount += base_sign * trade.notional_base
            quote_amount += quote_sign * trade.notional_quote
        result.append((period.end_time, base_amount, quote_amount))
    return result


def price_trade(
    trade: XccyTrade,
    base_curve: ZeroCurve,
    quote_curve: ZeroCurve,
    spot_quote_per_base: float,
    cross_currency_basis: float,
    discounting_mode: DiscountingMode,
    reporting_currency: str,
    valuation_time: float = 0.0,
) -> float:
    reporting = reporting_currency.upper()
    if reporting not in {trade.base_currency, trade.quote_currency}:
        raise ValueError("reporting_currency must be one of the trade currencies")

    cashflows = _leg_cashflows(trade, base_curve, quote_curve, valuation_time)
    pv_quote = 0.0

    if discounting_mode == "native":
        pv_base = 0.0
        pv_quote_leg = 0.0
        for payment_time, base_amount, quote_amount in cashflows:
            pv_base += base_amount * base_curve.relative_discount(valuation_time, payment_time)
            pv_quote_leg += quote_amount * quote_curve.relative_discount(
                valuation_time, payment_time
            )
        pv_quote = pv_base * spot_quote_per_base + pv_quote_leg

    elif discounting_mode == "quote_collateral":
        for payment_time, base_amount, quote_amount in cashflows:
            forward = fx_forward(
                spot_quote_per_base,
                base_curve,
                quote_curve,
                cross_currency_basis,
                payment_time,
                valuation_time,
            )
            quote_df = quote_curve.relative_discount(valuation_time, payment_time)
            pv_quote += (base_amount * forward + quote_amount) * quote_df

    elif discounting_mode == "base_collateral":
        pv_base = 0.0
        for payment_time, base_amount, quote_amount in cashflows:
            forward = fx_forward(
                spot_quote_per_base,
                base_curve,
                quote_curve,
                cross_currency_basis,
                payment_time,
                valuation_time,
            )
            base_df = base_curve.relative_discount(valuation_time, payment_time)
            pv_base += (base_amount + quote_amount / forward) * base_df
        pv_quote = pv_base * spot_quote_per_base

    else:
        raise ValueError(f"Unsupported discounting mode: {discounting_mode}")

    return pv_quote if reporting == trade.quote_currency else pv_quote / spot_quote_per_base


def deterministic_forward_profile(
    trade: XccyTrade,
    base_curve: ZeroCurve,
    quote_curve: ZeroCurve,
    spot: float,
    basis: float,
    mode: DiscountingMode,
    reporting_currency: str,
) -> list[dict[str, float | str]]:
    points: list[dict[str, float | str]] = []
    exposure_times = [0.0] + [period.end_time for period in trade.periods]
    exposure_dates = [trade.as_of_date] + [period.end_date for period in trade.periods]
    for index, (time, point_date) in enumerate(zip(exposure_times, exposure_dates)):
        if index == len(exposure_times) - 1:
            mtm = 0.0
        else:
            forward_spot = fx_forward(spot, base_curve, quote_curve, basis, time)
            mtm = price_trade(
                trade,
                base_curve,
                quote_curve,
                forward_spot,
                basis,
                mode,
                reporting_currency,
                valuation_time=time,
            )
        points.append({"date": point_date.isoformat(), "time": time, "mtm": mtm})
    return points


def _cholesky_correlated_normals(
    rng: random.Random,
    rho_fx_base: float,
    rho_fx_quote: float,
    rho_base_quote: float,
) -> tuple[float, float, float]:
    for value in (rho_fx_base, rho_fx_quote, rho_base_quote):
        if not -0.999 <= value <= 0.999:
            raise ValueError("Correlations must be between -0.999 and 0.999")

    l10 = rho_fx_base
    l11_sq = 1.0 - l10 * l10
    if l11_sq <= 0:
        raise ValueError("Invalid FX/base-rate correlation matrix")
    l11 = math.sqrt(l11_sq)
    l20 = rho_fx_quote
    l21 = (rho_base_quote - l20 * l10) / l11
    l22_sq = 1.0 - l20 * l20 - l21 * l21
    if l22_sq <= 1e-12:
        raise ValueError("Correlation matrix must be positive definite")
    l22 = math.sqrt(l22_sq)

    z0 = rng.gauss(0.0, 1.0)
    z1 = rng.gauss(0.0, 1.0)
    z2 = rng.gauss(0.0, 1.0)
    return z0, l10 * z0 + l11 * z1, l20 * z0 + l21 * z1 + l22 * z2


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _collateralized_exposure(mtm: float, threshold: float, mta: float) -> tuple[float, float]:
    required = math.copysign(max(abs(mtm) - threshold, 0.0), mtm) if mtm else 0.0
    if abs(required) < mta:
        required = 0.0
    return mtm - required, required


def simulate_exposure_profile(
    trade: XccyTrade,
    base_curve: ZeroCurve,
    quote_curve: ZeroCurve,
    spot: float,
    basis: float,
    mode: DiscountingMode,
    reporting_currency: str,
    assumptions: SimulationAssumptions,
) -> list[dict[str, float | str]]:
    if assumptions.paths < 1:
        raise ValueError("simulation paths must be positive")
    if not 0.5 < assumptions.pfe_quantile < 1.0:
        raise ValueError("pfe_quantile must be between 0.5 and 1")

    times = [0.0] + [period.end_time for period in trade.periods]
    dates = [trade.as_of_date] + [period.end_date for period in trade.periods]
    exposures: list[list[float]] = [[] for _ in times]
    collateral_balances: list[list[float]] = [[] for _ in times]
    rng = random.Random(assumptions.seed)

    clean_pv = price_trade(
        trade, base_curve, quote_curve, spot, basis, mode, reporting_currency
    )
    net, collateral = _collateralized_exposure(
        clean_pv, assumptions.collateral_threshold, assumptions.minimum_transfer_amount
    )
    exposures[0] = [net] * assumptions.paths
    collateral_balances[0] = [collateral] * assumptions.paths

    for _ in range(assumptions.paths):
        brownian_fx = 0.0
        brownian_base = 0.0
        brownian_quote = 0.0
        previous_time = 0.0
        for index, time in enumerate(times[1:], start=1):
            dt = max(time - previous_time, 0.0)
            z_fx, z_base, z_quote = _cholesky_correlated_normals(
                rng,
                assumptions.fx_base_rate_correlation,
                assumptions.fx_quote_rate_correlation,
                assumptions.base_quote_rate_correlation,
            )
            root_dt = math.sqrt(dt)
            brownian_fx += root_dt * z_fx
            brownian_base += root_dt * z_base
            brownian_quote += root_dt * z_quote

            deterministic_spot = fx_forward(spot, base_curve, quote_curve, basis, time)
            scenario_spot = deterministic_spot * math.exp(
                -0.5 * assumptions.fx_volatility**2 * time
                + assumptions.fx_volatility * brownian_fx
            )
            scenario_base_curve = base_curve.parallel_bump(
                assumptions.base_rate_volatility * brownian_base
            )
            scenario_quote_curve = quote_curve.parallel_bump(
                assumptions.quote_rate_volatility * brownian_quote
            )

            if index == len(times) - 1:
                scenario_mtm = 0.0
            else:
                scenario_mtm = price_trade(
                    trade,
                    scenario_base_curve,
                    scenario_quote_curve,
                    scenario_spot,
                    basis,
                    mode,
                    reporting_currency,
                    valuation_time=time,
                )
            scenario_net, scenario_collateral = _collateralized_exposure(
                scenario_mtm,
                assumptions.collateral_threshold,
                assumptions.minimum_transfer_amount,
            )
            exposures[index].append(scenario_net)
            collateral_balances[index].append(scenario_collateral)
            previous_time = time

    profile: list[dict[str, float | str]] = []
    forward_profile = deterministic_forward_profile(
        trade, base_curve, quote_curve, spot, basis, mode, reporting_currency
    )
    for index, (time, point_date) in enumerate(zip(times, dates)):
        values = exposures[index]
        positive = [max(value, 0.0) for value in values]
        negative = [max(-value, 0.0) for value in values]
        collateral_values = collateral_balances[index]
        profile.append(
            {
                "date": point_date.isoformat(),
                "time": time,
                "forward_mtm": float(forward_profile[index]["mtm"]),
                "epe": sum(positive) / len(positive),
                "ene": -(sum(negative) / len(negative)),
                "pfe": _quantile(positive, assumptions.pfe_quantile),
                "expected_collateral": sum(abs(value) for value in collateral_values)
                / len(collateral_values),
            }
        )
    return profile


def calculate_xva(
    clean_pv: float,
    profile: list[dict[str, float | str]],
    reporting_curve: ZeroCurve,
    assumptions: XvaAssumptions,
) -> dict[str, float]:
    if not 0.0 <= assumptions.counterparty_recovery < 1.0:
        raise ValueError("counterparty recovery must be in [0, 1)")
    if not 0.0 <= assumptions.own_recovery < 1.0:
        raise ValueError("own recovery must be in [0, 1)")

    cp_lgd = 1.0 - assumptions.counterparty_recovery
    own_lgd = 1.0 - assumptions.own_recovery
    cp_hazard = max(assumptions.counterparty_spread, 0.0) / max(cp_lgd, 1e-12)
    own_hazard = max(assumptions.own_spread, 0.0) / max(own_lgd, 1e-12)

    cva = dva = fva = colva = 0.0
    previous_time = 0.0
    previous_cp_survival = 1.0
    previous_own_survival = 1.0
    for point in profile[1:]:
        time = float(point["time"])
        dt = max(time - previous_time, 0.0)
        discount = reporting_curve.discount(time)
        cp_survival = math.exp(-cp_hazard * time)
        own_survival = math.exp(-own_hazard * time)
        cp_default_probability = previous_cp_survival - cp_survival
        own_default_probability = previous_own_survival - own_survival
        epe = float(point["epe"])
        ene_magnitude = -float(point["ene"])
        collateral = float(point["expected_collateral"])

        cva += cp_lgd * epe * cp_default_probability * discount
        dva += own_lgd * ene_magnitude * own_default_probability * discount
        fva += assumptions.funding_spread * epe * dt * discount
        colva += assumptions.collateral_spread * collateral * dt * discount

        previous_time = time
        previous_cp_survival = cp_survival
        previous_own_survival = own_survival

    total_xva = -cva + dva - fva - colva
    return {
        "cva": cva,
        "dva": dva,
        "fva": fva,
        "colva": colva,
        "total_xva": total_xva,
        "xva_adjusted_pv": clean_pv + total_xva,
    }


def cross_gamma_diagnostics(
    trade: XccyTrade,
    base_curve: ZeroCurve,
    quote_curve: ZeroCurve,
    spot: float,
    basis: float,
    mode: DiscountingMode,
    reporting_currency: str,
    fx_relative_bump: float = 0.01,
    rate_bump: float = 0.0001,
) -> dict[str, float]:
    base_pv = price_trade(
        trade, base_curve, quote_curve, spot, basis, mode, reporting_currency
    )
    spot_up = spot * (1.0 + fx_relative_bump)

    def interaction(curve_name: str) -> tuple[float, float]:
        bumped_base = base_curve.parallel_bump(rate_bump) if curve_name == "base" else base_curve
        bumped_quote = (
            quote_curve.parallel_bump(rate_bump) if curve_name == "quote" else quote_curve
        )
        pv_fx = price_trade(
            trade, base_curve, quote_curve, spot_up, basis, mode, reporting_currency
        )
        pv_rate = price_trade(
            trade,
            bumped_base,
            bumped_quote,
            spot,
            basis,
            mode,
            reporting_currency,
        )
        pv_both = price_trade(
            trade,
            bumped_base,
            bumped_quote,
            spot_up,
            basis,
            mode,
            reporting_currency,
        )
        interaction_pnl = pv_both - pv_fx - pv_rate + base_pv
        normalized = interaction_pnl / max(spot * fx_relative_bump * rate_bump, 1e-18)
        return interaction_pnl, normalized

    base_interaction, base_normalized = interaction("base")
    quote_interaction, quote_normalized = interaction("quote")
    return {
        "fx_bump_relative": fx_relative_bump,
        "rate_bump_decimal": rate_bump,
        "fx_base_discount_interaction_pnl": base_interaction,
        "fx_quote_discount_interaction_pnl": quote_interaction,
        "fx_base_discount_cross_gamma": base_normalized,
        "fx_quote_discount_cross_gamma": quote_normalized,
    }
