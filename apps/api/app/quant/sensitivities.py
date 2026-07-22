from __future__ import annotations

from dataclasses import replace

from .curves import ZeroCurve
from .xccy import (
    DiscountingMode,
    XccyTrade,
    XvaAssumptions,
    calculate_xva,
    price_trade,
)


def _central_delta(up: float, down: float, bump_units: float) -> float:
    if bump_units <= 0:
        raise ValueError("Sensitivity bump must be positive")
    return (up - down) / (2.0 * bump_units)


def _central_gamma(base: float, up: float, down: float, bump_units: float) -> float:
    if bump_units <= 0:
        raise ValueError("Sensitivity bump must be positive")
    return (up - 2.0 * base + down) / (bump_units * bump_units)


def calculate_clean_pv_sensitivities(
    *,
    trade: XccyTrade,
    base_curve: ZeroCurve,
    quote_curve: ZeroCurve,
    spot: float,
    basis: float,
    mode: DiscountingMode,
    reporting_currency: str,
    rate_bump_bps: float = 1.0,
    fx_bump_relative: float = 0.01,
    basis_bump_bps: float = 1.0,
    spread_bump_bps: float = 1.0,
) -> dict[str, object]:
    bumps = {
        "rate_bump_bps": rate_bump_bps,
        "fx_bump_relative": fx_bump_relative,
        "basis_bump_bps": basis_bump_bps,
        "spread_bump_bps": spread_bump_bps,
    }
    for name, value in bumps.items():
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    def pv(
        *,
        trade_value: XccyTrade = trade,
        base_curve_value: ZeroCurve = base_curve,
        quote_curve_value: ZeroCurve = quote_curve,
        spot_value: float = spot,
        basis_value: float = basis,
    ) -> float:
        return price_trade(
            trade_value,
            base_curve_value,
            quote_curve_value,
            spot_value,
            basis_value,
            mode,
            reporting_currency,
        )

    base_pv = pv()
    rate_bump_decimal = rate_bump_bps / 10_000.0

    def curve_sensitivities(role: str, curve: ZeroCurve) -> list[dict[str, float | str]]:
        points: list[dict[str, float | str]] = []
        for index, node in enumerate(curve.nodes):
            up_curve = curve.node_bump(index, rate_bump_decimal)
            down_curve = curve.node_bump(index, -rate_bump_decimal)
            if role == "base":
                up_pv = pv(base_curve_value=up_curve)
                down_pv = pv(base_curve_value=down_curve)
            else:
                up_pv = pv(quote_curve_value=up_curve)
                down_pv = pv(quote_curve_value=down_curve)
            points.append(
                {
                    "curve_role": role,
                    "currency": curve.currency,
                    "tenor": node.tenor,
                    "time": node.time,
                    "quote_id": node.quote_id,
                    "zero_rate": node.zero_rate,
                    "bump_bps": rate_bump_bps,
                    "up_pnl": up_pv - base_pv,
                    "down_pnl": down_pv - base_pv,
                    "pv01": _central_delta(up_pv, down_pv, rate_bump_bps),
                    "gamma_per_bp2": _central_gamma(
                        base_pv, up_pv, down_pv, rate_bump_bps
                    ),
                }
            )
        return points

    base_buckets = curve_sensitivities("base", base_curve)
    quote_buckets = curve_sensitivities("quote", quote_curve)

    base_parallel_pv01 = _central_delta(
        pv(base_curve_value=base_curve.parallel_bump(rate_bump_decimal)),
        pv(base_curve_value=base_curve.parallel_bump(-rate_bump_decimal)),
        rate_bump_bps,
    )
    quote_parallel_pv01 = _central_delta(
        pv(quote_curve_value=quote_curve.parallel_bump(rate_bump_decimal)),
        pv(quote_curve_value=quote_curve.parallel_bump(-rate_bump_decimal)),
        rate_bump_bps,
    )

    spot_up = spot * (1.0 + fx_bump_relative)
    spot_down = spot * (1.0 - fx_bump_relative)
    if spot_down <= 0:
        raise ValueError("FX sensitivity bump produces a non-positive spot")
    fx_up_pv = pv(spot_value=spot_up)
    fx_down_pv = pv(spot_value=spot_down)
    fx_delta_relative = _central_delta(fx_up_pv, fx_down_pv, fx_bump_relative)
    fx_gamma_relative = _central_gamma(
        base_pv, fx_up_pv, fx_down_pv, fx_bump_relative
    )
    spot_bump = spot * fx_bump_relative

    basis_bump_decimal = basis_bump_bps / 10_000.0
    basis_pv01 = _central_delta(
        pv(basis_value=basis + basis_bump_decimal),
        pv(basis_value=basis - basis_bump_decimal),
        basis_bump_bps,
    )

    spread_bump_decimal = spread_bump_bps / 10_000.0

    def leg_spread_pv01(field_name: str) -> float:
        current = getattr(trade, field_name)
        return _central_delta(
            pv(trade_value=replace(trade, **{field_name: current + spread_bump_decimal})),
            pv(trade_value=replace(trade, **{field_name: current - spread_bump_decimal})),
            spread_bump_bps,
        )

    return {
        "methodology": "central_bump_and_reprice_clean_pv",
        "reporting_currency": reporting_currency.upper(),
        "rate_bump_bps": rate_bump_bps,
        "fx_bump_relative": fx_bump_relative,
        "basis_bump_bps": basis_bump_bps,
        "base_curve": base_buckets,
        "quote_curve": quote_buckets,
        "summary": {
            "base_curve_parallel_pv01": base_parallel_pv01,
            "quote_curve_parallel_pv01": quote_parallel_pv01,
            "base_curve_bucket_sum_pv01": sum(
                float(point["pv01"]) for point in base_buckets
            ),
            "quote_curve_bucket_sum_pv01": sum(
                float(point["pv01"]) for point in quote_buckets
            ),
            "total_curve_parallel_pv01": base_parallel_pv01 + quote_parallel_pv01,
            "fx_delta_1pct": fx_delta_relative * 0.01,
            "fx_gamma_1pct2": fx_gamma_relative * 0.01 * 0.01,
            "fx_delta_per_spot_unit": _central_delta(
                fx_up_pv, fx_down_pv, spot_bump
            ),
            "fx_gamma_per_spot_unit2": _central_gamma(
                base_pv, fx_up_pv, fx_down_pv, spot_bump
            ),
            "cross_currency_basis_pv01": basis_pv01,
            "base_spread_pv01": leg_spread_pv01("base_spread"),
            "quote_spread_pv01": leg_spread_pv01("quote_spread"),
        },
    }


def calculate_xva_spread_sensitivities(
    *,
    clean_pv: float,
    profile: list[dict[str, float | str]],
    reporting_curve: ZeroCurve,
    assumptions: XvaAssumptions,
    spread_bump_bps: float = 1.0,
) -> dict[str, float]:
    if spread_bump_bps <= 0:
        raise ValueError("spread_bump_bps must be positive")
    bump_decimal = spread_bump_bps / 10_000.0

    def metric01(
        field_name: str,
        metric_name: str,
        *,
        allow_negative: bool = False,
    ) -> tuple[float, float]:
        current = float(getattr(assumptions, field_name))
        up_value = current + bump_decimal
        down_value = current - bump_decimal
        if not allow_negative:
            down_value = max(0.0, down_value)
        denominator_bps = (up_value - down_value) * 10_000.0
        if denominator_bps <= 0:
            raise ValueError(f"Invalid XVA sensitivity bump for {field_name}")
        up_metrics = calculate_xva(
            clean_pv,
            profile,
            reporting_curve,
            replace(assumptions, **{field_name: up_value}),
        )
        down_metrics = calculate_xva(
            clean_pv,
            profile,
            reporting_curve,
            replace(assumptions, **{field_name: down_value}),
        )
        return (
            (up_metrics[metric_name] - down_metrics[metric_name]) / denominator_bps,
            (up_metrics["total_xva"] - down_metrics["total_xva"])
            / denominator_bps,
        )

    cp_cva01, cp_total01 = metric01("counterparty_spread", "cva")
    own_dva01, own_total01 = metric01("own_spread", "dva")
    funding_fva01, funding_total01 = metric01("funding_spread", "fva")
    collateral_colva01, collateral_total01 = metric01(
        "collateral_spread", "colva", allow_negative=True
    )
    return {
        "spread_bump_bps": spread_bump_bps,
        "counterparty_cva01": cp_cva01,
        "counterparty_total_xva01": cp_total01,
        "own_dva01": own_dva01,
        "own_total_xva01": own_total01,
        "funding_fva01": funding_fva01,
        "funding_total_xva01": funding_total01,
        "collateral_colva01": collateral_colva01,
        "collateral_total_xva01": collateral_total01,
    }
