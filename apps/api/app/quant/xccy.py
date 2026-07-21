from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

from .conventions import (
    add_months,
    adjust_business_day,
    currency_convention,
    generate_schedule,
    year_fraction,
)
from .curves import DiscountCurve, date_to_curve_time
from .market import CanonicalMarket, ResolvedValue


DiscountingMode = Literal["native_ois", "base_collateral", "quote_collateral"]
LegType = Literal["fixed", "floating"]


@dataclass(frozen=True)
class LegTerms:
    leg_type: LegType = "floating"
    fixed_rate: float | None = None
    spread_bps: float = 0.0
    payment_frequency_months: int = 3


@dataclass(frozen=True)
class SwapTerms:
    as_of_date: date
    start_date: date
    maturity_date: date
    base_currency: str
    quote_currency: str
    base_notional: float
    quote_notional: float | None
    pay_base: bool
    base_leg: LegTerms
    quote_leg: LegTerms
    exchange_initial: bool
    exchange_final: bool
    reporting_currency: str
    discounting_mode: DiscountingMode


@dataclass(frozen=True)
class SimulationTerms:
    paths: int = 500
    time_steps_per_year: int = 4
    seed: int = 42
    fx_volatility_override: float | None = None
    base_rate_volatility_bps: float = 75.0
    quote_rate_volatility_bps: float = 75.0
    basis_volatility_bps: float = 20.0
    rate_mean_reversion: float = 0.05
    basis_mean_reversion: float = 0.10
    pfe_quantile: float = 0.95
    fx_base_rate_correlation: float = -0.20
    fx_quote_rate_correlation: float = 0.20
    base_quote_rate_correlation: float = 0.50
    fx_basis_correlation: float = 0.10
    base_basis_correlation: float = 0.20
    quote_basis_correlation: float = 0.20


@dataclass(frozen=True)
class CsaTerms:
    threshold: float = 0.0
    minimum_transfer_amount: float = 0.0
    collateral_haircut: float = 0.0
    margin_period_of_risk_days: int = 10
    collateral_spread_bps: float = 0.0


@dataclass(frozen=True)
class CreditTerms:
    hazard_rate: float = 0.01
    recovery_rate: float = 0.40


@dataclass(frozen=True)
class FundingTerms:
    funding_spread_bps: float = 50.0
    initial_margin_funding_spread_bps: float = 60.0
    capital_cost_rate: float = 0.10
    capital_multiplier: float = 0.08


@dataclass(frozen=True)
class Cashflow:
    payment_date: date
    currency: str
    leg: Literal["base", "quote"]
    kind: str
    amount: float
    accrual_start: date | None = None
    accrual_end: date | None = None
    accrual_year_fraction: float | None = None


@dataclass(frozen=True)
class ValuationResult:
    mode: DiscountingMode
    pv: float
    base_leg_pv: float
    quote_leg_pv: float


@dataclass(frozen=True)
class ExposurePoint:
    profile_date: date
    year_fraction: float
    forward_mtm: float
    expected_mtm: float
    epe: float
    ene: float
    pfe: float
    collateralized_epe: float
    collateralized_ene: float
    expected_collateral: float
    initial_margin_proxy: float
    discount_factor: float

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["date"] = payload.pop("profile_date")
        return payload


class XccySwapEngine:
    """Constant-notional G10 XCCY pricing and deterministic exposure MVP.

    The engine supports fixed or projected-floating coupons, three explicit
    discounting representations, mixed FX/rate/basis gamma diagnostics and a
    correlated four-factor simulation. It deliberately does not hide the model
    limitations: weekend-only calendars, single-curve projection and no notional
    reset are all returned as warnings.
    """

    MODEL_VERSION = "xccy-eod-mvp-0.2"
    MODES: tuple[DiscountingMode, ...] = (
        "native_ois",
        "base_collateral",
        "quote_collateral",
    )

    def __init__(
        self,
        market: CanonicalMarket,
        swap: SwapTerms,
        simulation: SimulationTerms,
        csa: CsaTerms,
        counterparty: CreditTerms,
        own_credit: CreditTerms,
        funding: FundingTerms,
        basis_override_bps: float | None = None,
    ) -> None:
        self.market = market
        self.swap = swap
        self.simulation = simulation
        self.csa = csa
        self.counterparty = counterparty
        self.own_credit = own_credit
        self.funding = funding

        self.base_currency = swap.base_currency.upper()
        self.quote_currency = swap.quote_currency.upper()
        self.reporting_currency = swap.reporting_currency.upper()
        if self.base_currency == self.quote_currency:
            raise ValueError("XCCY swap currencies must be different")
        currency_convention(self.base_currency)
        currency_convention(self.quote_currency)
        if self.reporting_currency not in {self.base_currency, self.quote_currency}:
            raise ValueError("Reporting currency must be the base or quote currency")
        if swap.start_date < swap.as_of_date:
            # Started swaps are supported, but the historical initial exchange is omitted.
            pass
        if swap.maturity_date <= swap.as_of_date:
            raise ValueError("Maturity date must be after as-of date")
        if swap.maturity_date <= swap.start_date:
            raise ValueError("Maturity date must be after start date")
        if swap.base_notional <= 0:
            raise ValueError("Base notional must be positive")

        self.base_curve = market.curve(self.base_currency)
        self.quote_curve = market.curve(self.quote_currency)
        self.spot_resolution = market.spot(self.base_currency, self.quote_currency)
        self.spot = self.spot_resolution.value
        self.vol_resolution = market.fx_volatility(
            self.base_currency,
            self.quote_currency,
            simulation.fx_volatility_override,
        )
        self.fx_volatility = self.vol_resolution.value
        self.basis_resolution = market.xccy_basis(
            self.base_currency,
            self.quote_currency,
            basis_override_bps,
        )
        self.basis = self.basis_resolution.value
        self.quote_notional = swap.quote_notional or swap.base_notional * self.spot
        if self.quote_notional <= 0:
            raise ValueError("Quote notional must be positive")

        self._validate_terms()
        self._base_periods = self._build_periods(
            self.base_currency, self.swap.base_leg.payment_frequency_months
        )
        self._quote_periods = self._build_periods(
            self.quote_currency, self.swap.quote_leg.payment_frequency_months
        )

    def _validate_terms(self) -> None:
        for name, leg in (("base", self.swap.base_leg), ("quote", self.swap.quote_leg)):
            if leg.leg_type == "fixed" and leg.fixed_rate is None:
                raise ValueError(f"{name} fixed leg requires fixed_rate")
            if leg.payment_frequency_months <= 0 or 12 % leg.payment_frequency_months:
                raise ValueError(
                    f"{name} payment frequency must be a positive divisor of 12"
                )
        if not 100 <= self.simulation.paths <= 5000:
            raise ValueError("Simulation paths must be between 100 and 5000")
        if not 1 <= self.simulation.time_steps_per_year <= 12:
            raise ValueError("time_steps_per_year must be between 1 and 12")
        if not 0.50 <= self.simulation.pfe_quantile < 1.0:
            raise ValueError("pfe_quantile must be in [0.50, 1.0)")
        if not 0.0 <= self.csa.collateral_haircut < 1.0:
            raise ValueError("collateral_haircut must be in [0, 1)")
        for name, credit in (
            ("counterparty", self.counterparty),
            ("own", self.own_credit),
        ):
            if credit.hazard_rate < 0:
                raise ValueError(f"{name} hazard rate must be non-negative")
            if not 0 <= credit.recovery_rate < 1:
                raise ValueError(f"{name} recovery rate must be in [0, 1)")

    def _leg_signs(self) -> tuple[float, float]:
        return (-1.0, 1.0) if self.swap.pay_base else (1.0, -1.0)

    def _build_periods(
        self, currency: str, frequency_months: int
    ) -> tuple[tuple[date, date, date, float], ...]:
        convention = currency_convention(currency)
        payment_dates = generate_schedule(
            self.swap.start_date,
            self.swap.maturity_date,
            frequency_months,
            convention.business_day_convention,
        )
        periods: list[tuple[date, date, date, float]] = []
        accrual_start = self.swap.start_date
        for payment_date in payment_dates:
            accrual_end = payment_date
            periods.append(
                (
                    accrual_start,
                    accrual_end,
                    payment_date,
                    year_fraction(accrual_start, accrual_end, convention.day_count),
                )
            )
            accrual_start = accrual_end
        return tuple(periods)

    def _curve_time(self, value: date) -> float:
        return date_to_curve_time(self.swap.as_of_date, value)

    def _coupon_cashflows(
        self,
        leg_name: Literal["base", "quote"],
        currency: str,
        notional: float,
        terms: LegTerms,
        sign: float,
        valuation_date: date,
        curve: DiscountCurve,
        curve_shift: float,
        periods: tuple[tuple[date, date, date, float], ...],
    ) -> list[Cashflow]:
        convention = currency_convention(currency)
        cashflows: list[Cashflow] = []
        valuation_t = self._curve_time(valuation_date)
        for accrual_start, accrual_end, payment_date, accrual in periods:
            if payment_date > valuation_date:
                if terms.leg_type == "fixed":
                    rate = float(terms.fixed_rate or 0.0)
                else:
                    start_t = max(self._curve_time(accrual_start), valuation_t)
                    end_t = self._curve_time(accrual_end)
                    projection_accrual = max(
                        year_fraction(
                            max(accrual_start, valuation_date),
                            accrual_end,
                            convention.day_count,
                        ),
                        1.0 / 365.0,
                    )
                    rate = curve.forward_rate(
                        start_t,
                        end_t,
                        projection_accrual,
                        curve_shift,
                    )
                rate += terms.spread_bps / 10_000.0
                cashflows.append(
                    Cashflow(
                        payment_date=payment_date,
                        currency=currency,
                        leg=leg_name,
                        kind=f"{terms.leg_type}_coupon",
                        amount=sign * notional * rate * accrual,
                        accrual_start=accrual_start,
                        accrual_end=accrual_end,
                        accrual_year_fraction=accrual,
                    )
                )
        return cashflows

    def _cashflows(
        self,
        valuation_date: date,
        base_shift: float = 0.0,
        quote_shift: float = 0.0,
    ) -> list[Cashflow]:
        base_sign, quote_sign = self._leg_signs()
        cashflows = self._coupon_cashflows(
            "base",
            self.base_currency,
            self.swap.base_notional,
            self.swap.base_leg,
            base_sign,
            valuation_date,
            self.base_curve,
            base_shift,
            self._base_periods,
        )
        cashflows.extend(
            self._coupon_cashflows(
                "quote",
                self.quote_currency,
                self.quote_notional,
                self.swap.quote_leg,
                quote_sign,
                valuation_date,
                self.quote_curve,
                quote_shift,
                self._quote_periods,
            )
        )

        start_date = adjust_business_day(self.swap.start_date)
        maturity_date = adjust_business_day(self.swap.maturity_date)
        if self.swap.exchange_initial and start_date > valuation_date:
            # Paying a leg's coupons means receiving its principal at inception.
            cashflows.extend(
                [
                    Cashflow(
                        start_date,
                        self.base_currency,
                        "base",
                        "initial_notional",
                        -base_sign * self.swap.base_notional,
                    ),
                    Cashflow(
                        start_date,
                        self.quote_currency,
                        "quote",
                        "initial_notional",
                        -quote_sign * self.quote_notional,
                    ),
                ]
            )
        if self.swap.exchange_final and maturity_date > valuation_date:
            cashflows.extend(
                [
                    Cashflow(
                        maturity_date,
                        self.base_currency,
                        "base",
                        "final_notional",
                        base_sign * self.swap.base_notional,
                    ),
                    Cashflow(
                        maturity_date,
                        self.quote_currency,
                        "quote",
                        "final_notional",
                        quote_sign * self.quote_notional,
                    ),
                ]
            )
        return sorted(cashflows, key=lambda item: (item.payment_date, item.leg, item.kind))

    def _convert_to_reporting(self, amount: float, currency: str, spot: float) -> float:
        if currency == self.reporting_currency:
            return amount
        if currency == self.base_currency and self.reporting_currency == self.quote_currency:
            return amount * spot
        if currency == self.quote_currency and self.reporting_currency == self.base_currency:
            return amount / spot
        raise ValueError("Unsupported reporting-currency conversion")

    def _forward_fx(
        self,
        valuation_t: float,
        payment_t: float,
        spot_at_valuation: float,
        base_shift: float,
        quote_shift: float,
        basis_shift: float,
    ) -> float:
        base_df = self.base_curve.discount_between(
            valuation_t, payment_t, base_shift
        )
        quote_df = self.quote_curve.discount_between(
            valuation_t, payment_t, quote_shift
        )
        dt = max(payment_t - valuation_t, 0.0)
        return spot_at_valuation * base_df / quote_df * math.exp(
            (self.basis + basis_shift) * dt
        )

    def value(
        self,
        mode: DiscountingMode,
        valuation_date: date | None = None,
        spot: float | None = None,
        base_shift: float = 0.0,
        quote_shift: float = 0.0,
        basis_shift: float = 0.0,
    ) -> ValuationResult:
        value_date = valuation_date or self.swap.as_of_date
        spot_value = float(spot if spot is not None else self.spot)
        if spot_value <= 0:
            raise ValueError("FX spot must be positive")
        valuation_t = self._curve_time(value_date)
        cashflows = self._cashflows(value_date, base_shift, quote_shift)

        base_native = 0.0
        quote_native = 0.0
        base_collateral_base_leg = 0.0
        base_collateral_quote_leg = 0.0
        quote_collateral_base_leg = 0.0
        quote_collateral_quote_leg = 0.0

        for cashflow in cashflows:
            payment_t = self._curve_time(cashflow.payment_date)
            base_df = self.base_curve.discount_between(
                valuation_t, payment_t, base_shift
            )
            quote_df = self.quote_curve.discount_between(
                valuation_t, payment_t, quote_shift
            )
            if cashflow.currency == self.base_currency:
                base_native += cashflow.amount * base_df
                forward = self._forward_fx(
                    valuation_t,
                    payment_t,
                    spot_value,
                    base_shift,
                    quote_shift,
                    basis_shift,
                )
                base_collateral_base_leg += cashflow.amount * base_df
                quote_collateral_base_leg += cashflow.amount * forward * quote_df
            else:
                quote_native += cashflow.amount * quote_df
                forward = self._forward_fx(
                    valuation_t,
                    payment_t,
                    spot_value,
                    base_shift,
                    quote_shift,
                    basis_shift,
                )
                base_collateral_quote_leg += cashflow.amount / forward * base_df
                quote_collateral_quote_leg += cashflow.amount * quote_df

        if mode == "native_ois":
            base_leg_pv = self._convert_to_reporting(
                base_native, self.base_currency, spot_value
            )
            quote_leg_pv = self._convert_to_reporting(
                quote_native, self.quote_currency, spot_value
            )
        elif mode == "base_collateral":
            base_leg_pv = self._convert_to_reporting(
                base_collateral_base_leg, self.base_currency, spot_value
            )
            quote_leg_pv = self._convert_to_reporting(
                base_collateral_quote_leg, self.base_currency, spot_value
            )
        elif mode == "quote_collateral":
            base_leg_pv = self._convert_to_reporting(
                quote_collateral_base_leg, self.quote_currency, spot_value
            )
            quote_leg_pv = self._convert_to_reporting(
                quote_collateral_quote_leg, self.quote_currency, spot_value
            )
        else:  # pragma: no cover - protected by Pydantic/Literal
            raise ValueError(f"Unsupported discounting mode: {mode}")
        return ValuationResult(mode, base_leg_pv + quote_leg_pv, base_leg_pv, quote_leg_pv)

    def deterministic_forward_spot(self, target_date: date) -> float:
        t = self._curve_time(target_date)
        return (
            self.spot
            * self.base_curve.discount_factor(t)
            / self.quote_curve.discount_factor(t)
            * math.exp(self.basis * t)
        )

    def _mode_diagnostics(self, mode: DiscountingMode) -> dict[str, object]:
        base = self.value(mode)
        fx_shock = self.spot * 0.01
        rate_shock = 0.0001
        pv = lambda **kwargs: self.value(mode, **kwargs).pv

        fx_up = pv(spot=self.spot + fx_shock)
        fx_down = pv(spot=self.spot - fx_shock)
        base_up = pv(base_shift=rate_shock)
        base_down = pv(base_shift=-rate_shock)
        quote_up = pv(quote_shift=rate_shock)
        quote_down = pv(quote_shift=-rate_shock)
        basis_up = pv(basis_shift=rate_shock)
        basis_down = pv(basis_shift=-rate_shock)

        def crossed_gamma(rate_key: str) -> float:
            kwargs_pp = {"spot": self.spot + fx_shock, rate_key: rate_shock}
            kwargs_pm = {"spot": self.spot + fx_shock, rate_key: -rate_shock}
            kwargs_mp = {"spot": self.spot - fx_shock, rate_key: rate_shock}
            kwargs_mm = {"spot": self.spot - fx_shock, rate_key: -rate_shock}
            return 0.25 * (
                pv(**kwargs_pp) - pv(**kwargs_pm) - pv(**kwargs_mp) + pv(**kwargs_mm)
            )

        return {
            "mode": mode,
            "pv": base.pv,
            "base_leg_pv": base.base_leg_pv,
            "quote_leg_pv": base.quote_leg_pv,
            "fx_delta_pnl_1pct": 0.5 * (fx_up - fx_down),
            "base_dv01": 0.5 * (base_up - base_down),
            "quote_dv01": 0.5 * (quote_up - quote_down),
            "basis_dv01": 0.5 * (basis_up - basis_down),
            "cross_gamma_fx_base_rate_pnl_1pct_1bp": crossed_gamma("base_shift"),
            "cross_gamma_fx_quote_rate_pnl_1pct_1bp": crossed_gamma("quote_shift"),
            "cross_gamma_fx_basis_pnl_1pct_1bp": crossed_gamma("basis_shift"),
        }

    def discounting_comparison(self) -> list[dict[str, object]]:
        rows = [self._mode_diagnostics(mode) for mode in self.MODES]
        selected = next(row for row in rows if row["mode"] == self.swap.discounting_mode)
        for row in rows:
            row["delta_to_selected"] = float(row["pv"]) - float(selected["pv"])
            row["cross_gamma_total_pnl_1pct_1bp"] = (
                float(row["cross_gamma_fx_base_rate_pnl_1pct_1bp"])
                + float(row["cross_gamma_fx_quote_rate_pnl_1pct_1bp"])
                + float(row["cross_gamma_fx_basis_pnl_1pct_1bp"])
            )
        return rows

    def _profile_dates(self) -> list[date]:
        months = max(1, round(12 / self.simulation.time_steps_per_year))
        dates = [self.swap.as_of_date]
        cursor = self.swap.as_of_date
        while True:
            cursor = add_months(cursor, months)
            if cursor >= self.swap.maturity_date:
                break
            dates.append(adjust_business_day(cursor))
        dates.append(adjust_business_day(self.swap.maturity_date))
        return sorted(set(dates))

    @staticmethod
    def _cholesky(matrix: list[list[float]]) -> list[list[float]]:
        size = len(matrix)
        result = [[0.0] * size for _ in range(size)]
        for i in range(size):
            for j in range(i + 1):
                subtotal = sum(result[i][k] * result[j][k] for k in range(j))
                if i == j:
                    diagonal = matrix[i][i] - subtotal
                    if diagonal <= 1e-12:
                        raise ValueError("Simulation correlation matrix is not positive definite")
                    result[i][j] = math.sqrt(diagonal)
                else:
                    result[i][j] = (matrix[i][j] - subtotal) / result[j][j]
        return result

    def _correlation_cholesky(self) -> list[list[float]]:
        s = self.simulation
        correlations = [
            [1.0, s.fx_base_rate_correlation, s.fx_quote_rate_correlation, s.fx_basis_correlation],
            [s.fx_base_rate_correlation, 1.0, s.base_quote_rate_correlation, s.base_basis_correlation],
            [s.fx_quote_rate_correlation, s.base_quote_rate_correlation, 1.0, s.quote_basis_correlation],
            [s.fx_basis_correlation, s.base_basis_correlation, s.quote_basis_correlation, 1.0],
        ]
        if any(abs(value) > 1.0 for row in correlations for value in row):
            raise ValueError("Correlations must be between -1 and 1")
        return self._cholesky(correlations)

    @staticmethod
    def _ou_step(
        value: float,
        mean_reversion: float,
        volatility: float,
        dt: float,
        normal: float,
    ) -> float:
        if mean_reversion <= 1e-12:
            return value + volatility * math.sqrt(dt) * normal
        decay = math.exp(-mean_reversion * dt)
        variance = (1.0 - math.exp(-2.0 * mean_reversion * dt)) / (
            2.0 * mean_reversion
        )
        return value * decay + volatility * math.sqrt(max(variance, 0.0)) * normal

    @staticmethod
    def _quantile(values: list[float], probability: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        position = (len(ordered) - 1) * probability
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    def _collateralized_value(self, pv: float) -> tuple[float, float]:
        threshold = max(self.csa.threshold, 0.0)
        mta = max(self.csa.minimum_transfer_amount, 0.0)
        unsecured_over_threshold = max(abs(pv) - threshold, 0.0)
        if unsecured_over_threshold < mta:
            collateral = 0.0
        else:
            collateral = unsecured_over_threshold * (1.0 - self.csa.collateral_haircut)
        residual = math.copysign(max(abs(pv) - collateral, 0.0), pv)
        return residual, collateral

    def exposure_profile(self) -> list[ExposurePoint]:
        dates = self._profile_dates()
        times = [self._curve_time(item) for item in dates]
        deterministic = [
            self.value(
                self.swap.discounting_mode,
                valuation_date=item,
                spot=self.deterministic_forward_spot(item),
            ).pv
            for item in dates
        ]
        path_values: list[list[float]] = [[] for _ in dates]
        collateralized_values: list[list[float]] = [[] for _ in dates]
        collateral_amounts: list[list[float]] = [[] for _ in dates]

        selected_pv = self.value(self.swap.discounting_mode).pv
        initial_residual, initial_collateral = self._collateralized_value(selected_pv)
        for _ in range(self.simulation.paths):
            path_values[0].append(selected_pv)
            collateralized_values[0].append(initial_residual)
            collateral_amounts[0].append(initial_collateral)

        rng = random.Random(self.simulation.seed)
        cholesky = self._correlation_cholesky()
        base_rate_vol = self.simulation.base_rate_volatility_bps / 10_000.0
        quote_rate_vol = self.simulation.quote_rate_volatility_bps / 10_000.0
        basis_vol = self.simulation.basis_volatility_bps / 10_000.0

        for _path in range(self.simulation.paths):
            log_spot = math.log(self.spot)
            base_shift = 0.0
            quote_shift = 0.0
            basis_shift = 0.0
            previous_t = times[0]
            for index in range(1, len(dates)):
                current_t = times[index]
                dt = max(current_t - previous_t, 1.0 / 365.0)
                independent = [rng.gauss(0.0, 1.0) for _ in range(4)]
                normals = [
                    sum(cholesky[row][col] * independent[col] for col in range(row + 1))
                    for row in range(4)
                ]
                base_shift = self._ou_step(
                    base_shift,
                    self.simulation.rate_mean_reversion,
                    base_rate_vol,
                    dt,
                    normals[1],
                )
                quote_shift = self._ou_step(
                    quote_shift,
                    self.simulation.rate_mean_reversion,
                    quote_rate_vol,
                    dt,
                    normals[2],
                )
                basis_shift = self._ou_step(
                    basis_shift,
                    self.simulation.basis_mean_reversion,
                    basis_vol,
                    dt,
                    normals[3],
                )
                base_rate = self.base_curve.zero_rate(previous_t, base_shift)
                quote_rate = self.quote_curve.zero_rate(previous_t, quote_shift)
                drift = quote_rate - base_rate + self.basis + basis_shift
                log_spot += (
                    drift - 0.5 * self.fx_volatility * self.fx_volatility
                ) * dt + self.fx_volatility * math.sqrt(dt) * normals[0]
                simulated_spot = math.exp(log_spot)
                pv = self.value(
                    self.swap.discounting_mode,
                    valuation_date=dates[index],
                    spot=simulated_spot,
                    base_shift=base_shift,
                    quote_shift=quote_shift,
                    basis_shift=basis_shift,
                ).pv
                residual, collateral = self._collateralized_value(pv)
                path_values[index].append(pv)
                collateralized_values[index].append(residual)
                collateral_amounts[index].append(collateral)
                previous_t = current_t

        reporting_curve = (
            self.base_curve
            if self.reporting_currency == self.base_currency
            else self.quote_curve
        )
        profile: list[ExposurePoint] = []
        for index, profile_date in enumerate(dates):
            values = path_values[index]
            residuals = collateralized_values[index]
            positive = [max(value, 0.0) for value in values]
            negative = [min(value, 0.0) for value in values]
            collateral_positive = [max(value, 0.0) for value in residuals]
            collateral_negative = [min(value, 0.0) for value in residuals]
            epe = sum(positive) / len(positive)
            ene = sum(negative) / len(negative)
            collateralized_epe = sum(collateral_positive) / len(collateral_positive)
            collateralized_ene = sum(collateral_negative) / len(collateral_negative)
            pfe = max(self._quantile(values, self.simulation.pfe_quantile), 0.0)
            profile.append(
                ExposurePoint(
                    profile_date=profile_date,
                    year_fraction=times[index],
                    forward_mtm=deterministic[index],
                    expected_mtm=sum(values) / len(values),
                    epe=epe,
                    ene=ene,
                    pfe=pfe,
                    collateralized_epe=collateralized_epe,
                    collateralized_ene=collateralized_ene,
                    expected_collateral=sum(collateral_amounts[index])
                    / len(collateral_amounts[index]),
                    initial_margin_proxy=(
                        max(pfe - epe, 0.0)
                        * math.sqrt(max(self.csa.margin_period_of_risk_days, 1) / 10.0)
                    ),
                    discount_factor=reporting_curve.discount_factor(times[index]),
                )
            )
        return profile

    def xva_metrics(self, profile: list[ExposurePoint]) -> dict[str, float]:
        cva = dva = fva = colva = mva = kva = 0.0
        cp_lgd = 1.0 - self.counterparty.recovery_rate
        own_lgd = 1.0 - self.own_credit.recovery_rate
        funding_spread = self.funding.funding_spread_bps / 10_000.0
        im_spread = self.funding.initial_margin_funding_spread_bps / 10_000.0
        collateral_spread = self.csa.collateral_spread_bps / 10_000.0

        for previous, current in zip(profile, profile[1:]):
            dt = max(current.year_fraction - previous.year_fraction, 0.0)
            midpoint_df = 0.5 * (previous.discount_factor + current.discount_factor)
            avg_epe = 0.5 * (
                previous.collateralized_epe + current.collateralized_epe
            )
            avg_ene = -0.5 * (
                previous.collateralized_ene + current.collateralized_ene
            )
            avg_collateral = 0.5 * (
                previous.expected_collateral + current.expected_collateral
            )
            avg_im = 0.5 * (
                previous.initial_margin_proxy + current.initial_margin_proxy
            )
            avg_uncollateralized_epe = 0.5 * (previous.epe + current.epe)

            cp_survival_previous = math.exp(
                -self.counterparty.hazard_rate * previous.year_fraction
            )
            cp_survival_current = math.exp(
                -self.counterparty.hazard_rate * current.year_fraction
            )
            own_survival_previous = math.exp(
                -self.own_credit.hazard_rate * previous.year_fraction
            )
            own_survival_current = math.exp(
                -self.own_credit.hazard_rate * current.year_fraction
            )
            cva += (
                cp_lgd
                * avg_epe
                * max(cp_survival_previous - cp_survival_current, 0.0)
                * midpoint_df
            )
            dva += (
                own_lgd
                * avg_ene
                * max(own_survival_previous - own_survival_current, 0.0)
                * midpoint_df
            )
            fva += funding_spread * avg_epe * dt * midpoint_df
            colva += collateral_spread * avg_collateral * dt * midpoint_df
            mva += im_spread * avg_im * dt * midpoint_df
            kva += (
                self.funding.capital_cost_rate
                * self.funding.capital_multiplier
                * avg_uncollateralized_epe
                * dt
                * midpoint_df
            )

        total = -cva + dva - fva - colva - mva - kva
        return {
            "cva": cva,
            "dva": dva,
            "fva": fva,
            "colva": colva,
            "mva": mva,
            "kva": kva,
            "total_xva": total,
        }

    def price(self) -> dict[str, object]:
        comparison = self.discounting_comparison()
        selected = next(
            row for row in comparison if row["mode"] == self.swap.discounting_mode
        )
        profile = self.exposure_profile()
        xva = self.xva_metrics(profile)
        pv = float(selected["pv"])
        warnings = [
            "Weekend-only business-day adjustment; named G10 holiday calendars are not yet loaded.",
            "OIS curves use piecewise-linear zero-rate pillars, not a full instrument bootstrap.",
            "Floating coupons use the same OIS curve for projection and discounting.",
            "Constant notionals only; mark-to-market/resettable XCCY notionals are not implemented.",
            "Exposure and XVA are deterministic-seed Monte Carlo diagnostics, not regulatory capital output.",
        ]
        for resolved in (
            self.spot_resolution,
            self.vol_resolution,
            self.basis_resolution,
        ):
            if resolved.warning:
                warnings.append(resolved.warning)

        maturity_forward = self.deterministic_forward_spot(self.swap.maturity_date)
        return {
            "model_version": self.MODEL_VERSION,
            "as_of_date": self.swap.as_of_date,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "reporting_currency": self.reporting_currency,
            "discounting_mode": self.swap.discounting_mode,
            "base_notional": self.swap.base_notional,
            "quote_notional": self.quote_notional,
            "pv": pv,
            "pv_with_xva": pv + xva["total_xva"],
            "cashflow_count": len(self._cashflows(self.swap.as_of_date)),
            "market": {
                "spot": self.spot_resolution.as_dict(),
                "maturity_forward_fx": maturity_forward,
                "fx_volatility": self.vol_resolution.as_dict(),
                "xccy_basis": self.basis_resolution.as_dict(),
                "base_curve": self.base_curve.pillars(),
                "quote_curve": self.quote_curve.pillars(),
            },
            "discounting_comparison": comparison,
            "greeks": {
                key: value
                for key, value in selected.items()
                if key
                not in {
                    "mode",
                    "pv",
                    "base_leg_pv",
                    "quote_leg_pv",
                    "delta_to_selected",
                }
            },
            "exposure_profile": [point.as_dict() for point in profile],
            "xva": xva,
            "simulation": {
                "paths": self.simulation.paths,
                "seed": self.simulation.seed,
                "time_steps_per_year": self.simulation.time_steps_per_year,
                "pfe_quantile": self.simulation.pfe_quantile,
            },
            "warnings": warnings,
        }
