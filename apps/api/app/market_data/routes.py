from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..schemas import (
    AsOfQuotesResponse,
    IngestionResponse,
    QuoteResponse,
    SnapshotPolicy,
    SnapshotResolutionResponse,
    SnapshotResponse,
)
from ..security import require_admin_api_key
from .conversions import CONVERSION_DESCRIPTIONS
from .providers import CsvSettlementProvider, EcbSdmxProvider
from .service import MarketDataService


router = APIRouter(prefix="/api/v1/market-data", tags=["EOD Market Data"])


def _service(request: Request) -> MarketDataService:
    return MarketDataService(request.app.state.database)


@router.get("/conversions")
def list_conversions() -> dict[str, dict[str, str]]:
    return {
        name: {
            "base_formula": formula,
            "final_formula": f"({formula}) * factor + shift",
        }
        for name, formula in CONVERSION_DESCRIPTIONS.items()
    }


@router.get("/providers")
def list_providers() -> list[dict[str, object]]:
    return [
        {
            "name": "settlement_csv",
            "network_required": False,
            "description": "Generic exchange/vendor EOD settlement CSV import",
        },
        {
            "name": "ECB",
            "network_required": True,
            "description": "ECB SDMX EUR FX reference rates and €STR",
        },
    ]


@router.post(
    "/ingestions/settlements",
    response_model=IngestionResponse,
    dependencies=[Depends(require_admin_api_key)],
)
async def ingest_settlement_csv(
    request: Request,
    valuation_date: date = Query(...),
    replace: bool = Query(False),
) -> dict[str, object]:
    body = await request.body()
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Settlement file must be UTF-8 encoded",
        ) from exc

    provider = CsvSettlementProvider()
    quotes = provider.parse_text(text, valuation_date)
    result = _service(request).ingest_quotes(
        provider=provider.name,
        valuation_date=valuation_date,
        quotes=quotes,
        replace=replace,
    )
    return result.as_dict()


@router.post(
    "/ingestions/ecb",
    response_model=IngestionResponse,
    dependencies=[Depends(require_admin_api_key)],
)
def ingest_ecb(
    request: Request,
    valuation_date: date = Query(...),
    replace: bool = Query(False),
) -> dict[str, object]:
    settings = request.app.state.settings
    provider = EcbSdmxProvider(
        base_url=settings.ecb_base_url,
        timeout_seconds=settings.ecb_timeout_seconds,
        lookback_days=settings.ecb_lookback_days,
        max_staleness_days=settings.ecb_max_staleness_days,
    )
    quotes = provider.fetch(valuation_date)
    result = _service(request).ingest_quotes(
        provider=provider.name,
        valuation_date=valuation_date,
        quotes=quotes,
        replace=replace,
    )
    return result.as_dict()


@router.get("/snapshots", response_model=list[SnapshotResponse])
def list_snapshots(
    request: Request, limit: int = Query(30, ge=1, le=3650)
) -> list[dict[str, object]]:
    """List persisted EOD snapshots, newest first."""
    return _service(request).list_snapshots(limit)


@router.get("/snapshots/{valuation_date}", response_model=SnapshotResponse)
def get_snapshot(request: Request, valuation_date: date) -> dict[str, object]:
    snapshot = _service(request).get_snapshot(valuation_date)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Market-data snapshot not found")
    return snapshot


@router.get(
    "/snapshots/{valuation_date}/quotes", response_model=list[QuoteResponse]
)
def get_snapshot_quotes(request: Request, valuation_date: date) -> list[dict[str, object]]:
    quotes = _service(request).get_quotes(valuation_date)
    if quotes is None:
        raise HTTPException(status_code=404, detail="Market-data snapshot not found")
    return quotes


@router.get("/as-of/{as_of_date}", response_model=SnapshotResolutionResponse)
def resolve_as_of_snapshot(
    request: Request,
    as_of_date: date,
    policy: SnapshotPolicy = Query(
        "exact",
        description=(
            "exact requires that date; previous_ready resolves the latest READY EOD "
            "snapshot on or before the requested date."
        ),
    ),
) -> dict[str, object]:
    resolution = _service(request).resolve_snapshot(as_of_date, policy)
    if resolution is None:
        detail = (
            f"Ready market-data snapshot not found for {as_of_date}"
            if policy == "exact"
            else f"No ready market-data snapshot found on or before {as_of_date}"
        )
        raise HTTPException(status_code=404, detail=detail)
    return resolution


@router.get("/as-of/{as_of_date}/quotes", response_model=AsOfQuotesResponse)
def get_as_of_quotes(
    request: Request,
    as_of_date: date,
    policy: SnapshotPolicy = Query("exact"),
) -> dict[str, object]:
    service = _service(request)
    resolution = service.resolve_snapshot(as_of_date, policy)
    if resolution is None:
        detail = (
            f"Ready market-data snapshot not found for {as_of_date}"
            if policy == "exact"
            else f"No ready market-data snapshot found on or before {as_of_date}"
        )
        raise HTTPException(status_code=404, detail=detail)

    market_data_date = resolution["market_data_date"]
    quotes = service.get_quotes(market_data_date)
    if quotes is None:  # pragma: no cover - defensive consistency check
        raise HTTPException(status_code=500, detail="Resolved snapshot quotes are unavailable")
    return {**resolution, "quotes": quotes}
