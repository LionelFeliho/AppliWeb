from __future__ import annotations

import math
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .market_data.service import MarketDataService
from .quant.conventions import (
    DEFAULT_CURVE_TENORS,
    G10_CURRENCIES,
    currency_convention,
    default_curve_quote_ids,
    parse_tenor_years,
)
from .quant.curves import CurveNode, ZeroCurve
from .quant.xccy import (
    SimulationAssumptions,
    XvaAssumptions,
    build_trade,
    calculate_xva,
    cross_gamma_diagnostics,
    price_trade,
    simulate_exposure_profile,
)
from .xccy_schemas import (
    CurveNodeRequest,
    CurveRequest,
    SnapshotPolicy,
    XccySwapRequest,
    XccySwapResponse,
)


pricing_router = APIRouter(prefix="/api/v1/pricing", tags=["Pricing"])
reference_router = APIRouter(prefix="/api/v1/reference", tags=["Reference Data"])
market_router = APIRouter(prefix="/api/v1/market-data", tags=["EOD Market Data"])


@reference_router.get("/g10")
def get_g10_reference() -> dict[str, object]:
    return {
        "currencies": [
            {
                **convention.as_dict(),
                "default_curve_nodes": default_curve_quote_ids(code),
            }
            for code, convention in sorted(G10_CURRENCIES.items())
        ],
        "fx_canonical_id": "FX.<BASE><QUOTE>.SPOT",
        "fx_quote_orientation": "quote currency units per one base currency unit",
        "curve_interpolation": "log-linear discount factors",
        "calendar_scope": (
            "Currency calendar names are registered, but the current schedule engine applies "
            "weekends only until audited holiday files are installed."
        ),
    }


@market_router.get("/completeness/g10/{as_of_date}")
def g10_market_completeness(
    as_of_date: date,
    request: Request,
    policy: SnapshotPolicy = "exact",
) -> dict[str, object]:
    service = MarketDataService(request.app.state.database)
    resolution = service.resolve_snapshot(as_of_date, policy)
    if resolution is None:
        detail = (
            f"Ready market-data snapshot not found for {as_of_date}"
            if policy == "exact"
            else f"No ready market-data snapshot found on or before {as_of_date}"
        )
        raise HTTPException(status_code=404, detail=detail)
    market_date = resolution["market_data_date"]

    missing_curves: dict[str, list[str]] = {}
    for currency in sorted(G10_CURRENCIES):
        missing = [
            item["quote_id"]
            for item in default_curve_quote_ids(currency)
            if service.get_quote(market_date, item["quote_id"]) is None
        ]
        if missing:
            missing_curves[currency] = missing

    fx_pillars = [
        "FX.EURUSD.SPOT",
        "FX.GBPUSD.SPOT",
        "FX.AUDUSD.SPOT",
        "FX.NZDUSD.SPOT",
        "FX.USDJPY.SPOT",
        "FX.USDCHF.SPOT",
        "FX.USDCAD.SPOT",
        "FX.USDNOK.SPOT",
        "FX.USDSEK.SPOT",
    ]
    missing_fx = [
        quote_id
        for quote_id in fx_pillars
        if service.get_quote(market_date, quote_id) is None
    ]
    complete = not missing_curves and not missing_fx
    return {
        "as_of_date": as_of_date,
        "market_data_date": market_date,
        "snapshot_policy": policy,
        "exact_snapshot": resolution["exact_snapshot"],
        "snapshot_checksum": resolution["snapshot"]["checksum"],
        "complete": complete,
        "required_curve_tenors": list(DEFAULT_CURVE_TENORS),
        "missing_curve_quotes": missing_curves,
        "missing_fx_quotes": missing_fx,
    }


def _market_data_resolution(
    service: MarketDataService, payload: XccySwapRequest
) -> dict[str, Any]:
    resolution = service.resolve_snapshot(payload.as_of_date, payload.snapshot_policy)
    if resolution is None:
        detail = (
            f"Ready market-data snapshot not found for {payload.as_of_date}"
            if payload.snapshot_policy == "exact"
            else f"No ready market-data snapshot found on or before {payload.as_of_date}"
        )
        raise HTTPException(status_code=404, detail=detail)
    return resolution


def _quote_value(
    service: MarketDataService,
    market_date,
    quote_id: str,
    allowed_types: set[str],
    expected_currency: str | None = None,
) -> tuple[float, dict[str, Any]]:
    quote = service.get_quote(market_date, quote_id)
    if quote is None:
        raise HTTPException(status_code=422, detail=f"Required market quote not found: {quote_id}")
    if quote["quote_type"] not in allowed_types:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Quote {quote_id} has type {quote['quote_type']}; expected one of "
                f"{', '.join(sorted(allowed_types))}"
            ),
        )
    if expected_currency:
        quote_currency = (quote.get("currency") or "").upper()
        if quote_currency and quote_currency != expected_currency.upper():
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Quote {quote_id} currency {quote_currency} does not match "
                    f"curve currency {expected_currency.upper()}"
                ),
            )
    value = float(quote["normalized_value"])
    if not math.isfinite(value):
        raise HTTPException(status_code=422, detail=f"Quote {quote_id} is not finite")
    return value, quote


def _curve_request(currency: str, request: CurveRequest | None) -> CurveRequest:
    code = currency.upper()
    if request is None:
        return CurveRequest(currency=code)
    if request.currency.upper() != code:
        raise HTTPException(
            status_code=422,
            detail=f"Curve currency {request.currency} does not match trade currency {code}",
        )
    return request


def _build_curve(
    service: MarketDataService,
    market_date,
    currency: str,
    request: CurveRequest | None,
) -> tuple[ZeroCurve, list[str]]:
    curve_request = _curve_request(currency, request)
    node_requests = curve_request.nodes or [
        CurveNodeRequest(**item) for item in default_curve_quote_ids(currency)
    ]
    nodes: list[CurveNode] = []
    supplied_tenors: set[str] = set()
    missing_quote_tenors: list[str] = []
    for node_request in node_requests:
        tenor = node_request.tenor.upper()
        time = parse_tenor_years(tenor)
        if service.get_quote(market_date, node_request.quote_id) is None:
            missing_quote_tenors.append(tenor)
            continue
        value, quote = _quote_value(
            service,
            market_date,
            node_request.quote_id,
            {"RATE", "YIELD", "DISCOUNT_FACTOR"},
            expected_currency=currency,
        )
        if quote["quote_type"] == "DISCOUNT_FACTOR":
            if not 0.0 < value <= 1.5:
                raise HTTPException(
                    status_code=422,
                    detail=f"Discount factor {node_request.quote_id} must be positive",
                )
            zero_rate = -math.log(value) / time
        else:
            zero_rate = value
        nodes.append(
            CurveNode(
                tenor=tenor,
                time=time,
                zero_rate=zero_rate,
                quote_id=node_request.quote_id,
            )
        )
        supplied_tenors.add(tenor)

    required = [tenor.upper() for tenor in curve_request.required_tenors]
    missing = sorted(
        set(tenor for tenor in required if tenor not in supplied_tenors)
        | set(missing_quote_tenors),
        key=lambda tenor: parse_tenor_years(tenor),
    )
    if missing and curve_request.strict_completeness:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Incomplete {currency.upper()} curve. Missing required tenors: "
                f"{', '.join(missing)}"
            ),
        )
    try:
        return ZeroCurve(currency, nodes), missing
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _direct_or_inverse_quote(
    service: MarketDataService,
    market_date,
    base: str,
    quote: str,
    direct_id: str,
    inverse_id: str,
) -> tuple[float, list[str], str] | None:
    direct = service.get_quote(market_date, direct_id)
    if direct is not None:
        value = float(direct["normalized_value"])
        if value <= 0:
            raise HTTPException(status_code=422, detail=f"FX quote {direct_id} must be positive")
        return value, [direct_id], "direct"
    inverse = service.get_quote(market_date, inverse_id)
    if inverse is not None:
        value = float(inverse["normalized_value"])
        if value <= 0:
            raise HTTPException(status_code=422, detail=f"FX quote {inverse_id} must be positive")
        return 1.0 / value, [inverse_id], "inverse"
    return None


def _resolve_fx_spot(
    service: MarketDataService,
    market_date,
    base: str,
    quote: str,
    explicit_quote_id: str | None,
) -> tuple[float, list[str], str]:
    base = base.upper()
    quote = quote.upper()
    if explicit_quote_id:
        value, _ = _quote_value(
            service, market_date, explicit_quote_id, {"SPOT", "FORWARD"}
        )
        if value <= 0:
            raise HTTPException(status_code=422, detail="FX spot must be positive")
        normalized_id = explicit_quote_id.upper()
        inverse_pair = f"{quote}{base}"
        direct_pair = f"{base}{quote}"
        if inverse_pair in normalized_id and direct_pair not in normalized_id:
            return 1.0 / value, [explicit_quote_id], "explicit_inverse"
        return value, [explicit_quote_id], "explicit_direct"

    direct = _direct_or_inverse_quote(
        service,
        market_date,
        base,
        quote,
        f"FX.{base}{quote}.SPOT",
        f"FX.{quote}{base}.SPOT",
    )
    if direct:
        return direct

    def usd_per_currency(currency: str) -> tuple[float, list[str], str]:
        if currency == "USD":
            return 1.0, [], "identity"
        result = _direct_or_inverse_quote(
            service,
            market_date,
            currency,
            "USD",
            f"FX.{currency}USD.SPOT",
            f"FX.USD{currency}.SPOT",
        )
        if result is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot resolve FX {base}{quote}: missing direct/inverse quote and "
                    f"USD pillar for {currency}"
                ),
            )
        return result

    usd_base, ids_base, path_base = usd_per_currency(base)
    usd_quote, ids_quote, path_quote = usd_per_currency(quote)
    return (
        usd_base / usd_quote,
        ids_base + ids_quote,
        f"usd_cross:{path_base}/{path_quote}",
    )


def _optional_decimal_quote(
    service: MarketDataService,
    market_date,
    quote_id: str | None,
    fallback: float,
    allowed_types: set[str],
) -> tuple[float, str]:
    if not quote_id:
        return fallback, "request"
    value, _ = _quote_value(service, market_date, quote_id, allowed_types)
    return value, quote_id


@pricing_router.post("/xccy-swap", response_model=XccySwapResponse)
def price_xccy_swap(payload: XccySwapRequest, request: Request) -> dict[str, object]:
    try:
        base_convention = currency_convention(payload.base_currency)
        quote_convention = currency_convention(payload.quote_currency)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = MarketDataService(request.app.state.database)
    resolution = _market_data_resolution(service, payload)
    market_date = resolution["market_data_date"]
    snapshot = resolution["snapshot"]

    base_curve, base_missing = _build_curve(
        service, market_date, payload.base_currency, payload.base_curve
    )
    quote_curve, quote_missing = _build_curve(
        service, market_date, payload.quote_currency, payload.quote_curve
    )
    spot, spot_ids, fx_resolution = _resolve_fx_spot(
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

    modes = [payload.discounting_mode]
    if payload.compare_discounting:
        modes = ["native", "base_collateral", "quote_collateral"]
    pv_by_discounting = {
        mode: price_trade(
            trade,
            base_curve,
            quote_curve,
            spot,
            basis,
            mode,
            reporting_currency,
        )
        for mode in modes
    }
    clean_pv = pv_by_discounting[payload.discounting_mode]
    discount_switch_impact = {
        mode: value - clean_pv for mode, value in pv_by_discounting.items()
    }

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
    try:
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
            base_curve if reporting_currency == payload.base_currency else quote_curve
        )
        xva = calculate_xva(
            clean_pv,
            profile,
            reporting_curve,
            XvaAssumptions(
                counterparty_spread=cp_spread,
                own_spread=own_spread,
                counterparty_recovery=payload.xva.counterparty_recovery,
                own_recovery=payload.xva.own_recovery,
                funding_spread=payload.xva.funding_spread_bps / 10_000.0,
                collateral_spread=payload.xva.collateral_spread_bps / 10_000.0,
            ),
        )
        cross_gamma = cross_gamma_diagnostics(
            trade,
            base_curve,
            quote_curve,
            spot,
            basis,
            payload.discounting_mode,
            reporting_currency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    base_curve_payload = base_curve.as_dict()
    base_curve_payload["missing_required_tenors"] = base_missing
    quote_curve_payload = quote_curve.as_dict()
    quote_curve_payload["missing_required_tenors"] = quote_missing

    return {
        "as_of_date": payload.as_of_date,
        "market_data_date": market_date,
        "snapshot_policy": payload.snapshot_policy,
        "exact_snapshot": resolution["exact_snapshot"],
        "snapshot_checksum": snapshot["checksum"],
        "base_currency": payload.base_currency,
        "quote_currency": payload.quote_currency,
        "reporting_currency": reporting_currency,
        "fx_spot": spot,
        "fx_spot_quote_ids": spot_ids,
        "fx_resolution": fx_resolution,
        "notional_base": payload.notional_base,
        "notional_quote": notional_quote,
        "selected_discounting_mode": payload.discounting_mode,
        "clean_pv": clean_pv,
        "pv_by_discounting": pv_by_discounting,
        "discount_switch_impact": discount_switch_impact,
        "cross_gamma": cross_gamma,
        "base_curve": base_curve_payload,
        "quote_curve": quote_curve_payload,
        "exposure_profile": profile,
        "xva": xva,
        "assumptions": {
            "curve_required_tenors": list(DEFAULT_CURVE_TENORS),
            "cross_currency_basis_bps": payload.cross_currency_basis_bps,
            "fx_volatility": fx_volatility,
            "fx_volatility_source": fx_vol_source,
            "counterparty_spread": cp_spread,
            "counterparty_spread_source": cp_spread_source,
            "own_spread": own_spread,
            "own_spread_source": own_spread_source,
            "paths": payload.simulation.paths,
            "seed": payload.simulation.seed,
            "base_rate_volatility": payload.simulation.base_rate_volatility,
            "quote_rate_volatility": payload.simulation.quote_rate_volatility,
            "fx_base_rate_correlation": payload.simulation.fx_base_rate_correlation,
            "fx_quote_rate_correlation": payload.simulation.fx_quote_rate_correlation,
            "base_quote_rate_correlation": payload.simulation.base_quote_rate_correlation,
            "pfe_quantile": payload.simulation.pfe_quantile,
            "collateral_threshold": payload.simulation.collateral_threshold,
            "minimum_transfer_amount": payload.simulation.minimum_transfer_amount,
            "counterparty_recovery": payload.xva.counterparty_recovery,
            "own_recovery": payload.xva.own_recovery,
            "funding_spread_bps": payload.xva.funding_spread_bps,
            "collateral_spread_bps": payload.xva.collateral_spread_bps,
        },
        "warnings": [
            "MVP uses one OIS curve per currency for both projection and discounting.",
            "Schedules currently apply weekend adjustment only; audited holiday files are pending.",
            "Exposure uses correlated lognormal FX and Gaussian parallel-rate shocks.",
            "CVA/DVA use flat hazard rates inferred from spreads; FVA/COLVA use deterministic spreads.",
            "Collateral is an instantaneous threshold/MTA approximation without margin-period lag.",
            "Results require independent benchmark validation before trading or accounting use.",
        ],
    }
