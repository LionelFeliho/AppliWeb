from __future__ import annotations

import math
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request

from .market_data.service import MarketDataService
from .schemas import ZeroCouponRequest, ZeroCouponResponse


router = APIRouter(prefix="/api/v1/pricing", tags=["Pricing"])


@router.post("/zero-coupon", response_model=ZeroCouponResponse)
def price_zero_coupon(payload: ZeroCouponRequest, request: Request) -> dict[str, object]:
    if payload.maturity_date <= payload.as_of_date:
        raise HTTPException(status_code=400, detail="maturity_date must be after as_of_date")

    service = MarketDataService(request.app.state.database)
    resolution = service.resolve_snapshot(payload.as_of_date, payload.snapshot_policy)
    if resolution is None:
        detail = (
            f"Ready market-data snapshot not found for {payload.as_of_date}"
            if payload.snapshot_policy == "exact"
            else f"No ready market-data snapshot found on or before {payload.as_of_date}"
        )
        raise HTTPException(status_code=404, detail=detail)

    market_data_date = resolution["market_data_date"]
    snapshot = resolution["snapshot"]
    quote = service.get_quote(market_data_date, payload.discount_rate_quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="Discount-rate quote not found")
    if quote["quote_type"] not in {"RATE", "YIELD"}:
        raise HTTPException(
            status_code=400,
            detail=f"Quote {payload.discount_rate_quote_id} is not a RATE or YIELD",
        )
    quote_currency = (quote.get("currency") or "").upper()
    requested_currency = payload.currency.upper()
    if quote_currency and quote_currency != requested_currency:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Discount-rate quote currency {quote_currency} does not match "
                f"cash-flow currency {requested_currency}"
            ),
        )

    rate = Decimal(quote["normalized_value"])
    year_fraction = Decimal(
        str((payload.maturity_date - payload.as_of_date).days / 365.0)
    )
    r = float(rate)
    t = float(year_fraction)

    if payload.compounding == "simple":
        denominator = 1.0 + r * t
        if denominator <= 0:
            raise HTTPException(status_code=400, detail="Invalid simple-compounding denominator")
        discount_factor_float = 1.0 / denominator
    elif payload.compounding == "annual":
        if 1.0 + r <= 0:
            raise HTTPException(status_code=400, detail="Invalid annual-compounding base")
        discount_factor_float = math.pow(1.0 + r, -t)
    else:
        discount_factor_float = math.exp(-r * t)

    discount_factor = Decimal(str(discount_factor_float))
    present_value = payload.notional * discount_factor

    return {
        "as_of_date": payload.as_of_date,
        "market_data_date": market_data_date,
        "snapshot_policy": payload.snapshot_policy,
        "exact_snapshot": resolution["exact_snapshot"],
        "maturity_date": payload.maturity_date,
        "currency": requested_currency,
        "notional": payload.notional,
        "discount_rate_quote_id": payload.discount_rate_quote_id,
        "discount_rate": rate,
        "year_fraction_act_365f": year_fraction,
        "compounding": payload.compounding,
        "discount_factor": discount_factor,
        "present_value": present_value,
        "snapshot_checksum": snapshot["checksum"],
    }
