from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .market_data.service import MarketDataService
from .quant.conventions import currency_convention
from .quant.sensitivities import (
    calculate_clean_pv_sensitivities,
    calculate_xva_spread_sensitivities,
)
from .quant.xccy import (
    SimulationAssumptions,
    XvaAssumptions,
    build_trade,
    price_trade,
    simulate_exposure_profile,
)
from .xccy_routes import (
    _build_curve,
    _market_data_resolution,
    _optional_decimal_quote,
    _resolve_fx_spot,
)
from .xccy_schemas import XccySensitivityAnalysisResponse, XccySwapRequest


router = APIRouter(prefix="/api/v1/pricing", tags=["Sensitivities"])


@router.post(
    "/xccy-swap/sensitivities",
    response_model=XccySensitivityAnalysisResponse,
)
def xccy_swap_sensitivities(
    payload: XccySwapRequest,
    request: Request,
) -> dict[str, object]:
    try:
        base_convention = currency_convention(payload.base_currency)
        quote_convention = currency_convention(payload.quote_currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = MarketDataService(request.app.state.database)
    resolution = _market_data_resolution(service, payload)
    market_date = resolution["market_data_date"]
    snapshot = resolution["snapshot"]
    base_curve, _ = _build_curve(
        service,
        market_date,
        payload.base_currency,
        payload.base_curve,
    )
    quote_curve, _ = _build_curve(
        service,
        market_date,
        payload.quote_currency,
        payload.quote_curve,
    )
    spot, _, fx_resolution = _resolve_fx_spot(
        service,
        market_date,
        payload.base_currency,
        payload.quote_currency,
        payload.fx_spot_quote_id,
    )

    reporting_currency = (payload.reporting_currency or payload.quote_currency).upper()
    notional_quote = payload.notional_quote or payload.notional_base * spot
    basis = payload.cross_currency_basis_bps / 10_000.0
    trade = build_trade(
        as_of_date=payload.as_of_date,
        maturity_date=payload.maturity_date,
        base_currency=payload.base_currency,
        quote_currency=payload.quote_currency,
        notional_base=payload.notional_base,
        notional_quote=notional_quote,
        pay_base=payload.pay_base,
        base_spread=payload.base_spread_bps / 10_000.0,
        quote_spread=payload.quote_spread_bps / 10_000.0,
        payment_frequency_months=payload.payment_frequency_months,
        base_day_count=payload.base_day_count or base_convention.day_count,
        quote_day_count=payload.quote_day_count or quote_convention.day_count,
        exchange_notionals=payload.exchange_notionals,
    )

    fx_volatility, fx_vol_source = _optional_decimal_quote(
        service,
        market_date,
        payload.simulation.fx_vol_quote_id,
        payload.simulation.fx_volatility,
        {"VOLATILITY"},
    )
    cp_spread, cp_spread_source = _optional_decimal_quote(
        service,
        market_date,
        payload.xva.counterparty_spread_quote_id,
        payload.xva.counterparty_spread_bps / 10_000.0,
        {"SPREAD", "RATE"},
    )
    own_spread, own_spread_source = _optional_decimal_quote(
        service,
        market_date,
        payload.xva.own_spread_quote_id,
        payload.xva.own_spread_bps / 10_000.0,
        {"SPREAD", "RATE"},
    )

    simulation_assumptions = SimulationAssumptions(
        paths=payload.simulation.paths,
        seed=payload.simulation.seed,
        fx_volatility=fx_volatility,
        base_rate_volatility=payload.simulation.base_rate_volatility,
        quote_rate_volatility=payload.simulation.quote_rate_volatility,
        fx_base_rate_correlation=payload.simulation.fx_base_rate_correlation,
        fx_quote_rate_correlation=payload.simulation.fx_quote_rate_correlation,
        base_quote_rate_correlation=payload.simulation.base_quote_rate_correlation,
        pfe_quantile=payload.simulation.pfe_quantile,
        collateral_threshold=payload.simulation.collateral_threshold,
        minimum_transfer_amount=payload.simulation.minimum_transfer_amount,
    )
    xva_assumptions = XvaAssumptions(
        counterparty_spread=cp_spread,
        own_spread=own_spread,
        counterparty_recovery=payload.xva.counterparty_recovery,
        own_recovery=payload.xva.own_recovery,
        funding_spread=payload.xva.funding_spread_bps / 10_000.0,
        collateral_spread=payload.xva.collateral_spread_bps / 10_000.0,
    )

    try:
        clean_pv = price_trade(
            trade,
            base_curve,
            quote_curve,
            spot,
            basis,
            payload.discounting_mode,
            reporting_currency,
        )
        sensitivities = calculate_clean_pv_sensitivities(
            trade=trade,
            base_curve=base_curve,
            quote_curve=quote_curve,
            spot=spot,
            basis=basis,
            mode=payload.discounting_mode,
            reporting_currency=reporting_currency,
            rate_bump_bps=payload.sensitivities.rate_bump_bps,
            fx_bump_relative=payload.sensitivities.fx_bump_relative,
            basis_bump_bps=payload.sensitivities.basis_bump_bps,
            spread_bump_bps=payload.sensitivities.spread_bump_bps,
        )
        profile = simulate_exposure_profile(
            trade,
            base_curve,
            quote_curve,
            spot,
            basis,
            payload.discounting_mode,
            reporting_currency,
            simulation_assumptions,
        )
        reporting_curve = (
            base_curve
            if reporting_currency == payload.base_currency
            else quote_curve
        )
        sensitivities["xva"] = calculate_xva_spread_sensitivities(
            clean_pv=clean_pv,
            profile=profile,
            reporting_curve=reporting_curve,
            assumptions=xva_assumptions,
            spread_bump_bps=payload.sensitivities.spread_bump_bps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return {
        "as_of_date": payload.as_of_date,
        "market_data_date": market_date,
        "snapshot_policy": payload.snapshot_policy,
        "exact_snapshot": resolution["exact_snapshot"],
        "snapshot_checksum": snapshot["checksum"],
        "base_currency": payload.base_currency,
        "quote_currency": payload.quote_currency,
        "reporting_currency": reporting_currency,
        "selected_discounting_mode": payload.discounting_mode,
        "clean_pv": clean_pv,
        "sensitivities": sensitivities,
        "assumptions": {
            "fx_resolution": fx_resolution,
            "fx_volatility": fx_volatility,
            "fx_volatility_source": fx_vol_source,
            "counterparty_spread": cp_spread,
            "counterparty_spread_source": cp_spread_source,
            "own_spread": own_spread,
            "own_spread_source": own_spread_source,
            "paths": payload.simulation.paths,
            "seed": payload.simulation.seed,
        },
        "warnings": [
            "Clean-PV sensitivities use central bump-and-reprice on the selected discounting regime.",
            "XVA spread 01s hold the simulated exposure profile fixed and do not re-simulate market risk.",
            "Sensitivity results require independent benchmark validation before trading or accounting use.",
        ],
    }
