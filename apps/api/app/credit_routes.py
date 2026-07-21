from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Any, Iterable

from fastapi import APIRouter, HTTPException, Request

from .credit_schemas import (
    CreditBasketConstituentRequest,
    CrossSectionCalibrationSettings,
    NomuraBasketCurveRequest,
    NomuraBasketCurveResponse,
    NomuraCrossSectionCalibrationRequest,
    NomuraCrossSectionResponse,
    NomuraCrossSectionXvaRequest,
    NomuraCrossSectionXvaResponse,
)
from .market_data.service import MarketDataService
from .quant.conventions import parse_tenor_years
from .quant.credit_cross_section import (
    BasketConstituent,
    CreditObservation,
    build_basket_credit_curve,
    calculate_credit_xva,
    fit_cross_section_curve,
    normalize_category,
    normalize_rating,
    normalize_tenor,
)


router = APIRouter(
    prefix="/api/v1/credit/nomura-cross-section",
    tags=["Credit Cross Section"],
)

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "entity_id": ("entity_id", "obligor", "issuer", "reference_entity", "ticker", "short_name"),
    "tenor": ("tenor", "maturity", "cds_tenor", "term"),
    "sector": ("sector", "industry", "industry_sector"),
    "region": ("region", "geography", "country_region"),
    "rating": ("rating", "credit_rating", "composite_rating"),
    "seniority": ("seniority", "debt_seniority", "tier"),
    "contributors": ("contributors", "contributor_count", "num_contributors"),
    "liquidity_weight": ("liquidity_weight", "weight", "quote_weight"),
}


def _resolve_snapshot(
    service: MarketDataService, as_of_date: date, policy: str
) -> dict[str, Any]:
    resolution = service.resolve_snapshot(as_of_date, policy)
    if resolution is None:
        detail = (
            f"Ready market-data snapshot not found for {as_of_date}"
            if policy == "exact"
            else f"No ready market-data snapshot found on or before {as_of_date}"
        )
        raise HTTPException(status_code=404, detail=detail)
    return resolution


def _flatten_metadata(quote: dict[str, Any]) -> dict[str, Any]:
    metadata = dict(quote.get("metadata") or {})
    source_columns = metadata.pop("source_columns", None)
    if isinstance(source_columns, dict):
        metadata.update(source_columns)
    return {str(key).strip().lower(): value for key, value in metadata.items()}


def _metadata_value(metadata: dict[str, Any], field: str) -> Any:
    for alias in _FIELD_ALIASES[field]:
        value = metadata.get(alias)
        if value not in (None, ""):
            return value
    return None


def _infer_tenor(canonical_id: str) -> str | None:
    matches = re.findall(
        r"(?:^|[._-])([0-9]+(?:\.[0-9]+)?[DWMY])(?:$|[._-])",
        canonical_id.upper(),
    )
    return matches[-1] if matches else None


def _to_positive_float(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    result = float(value)
    if result <= 0.0:
        return default
    return result


def _extract_observations(
    quotes: Iterable[dict[str, Any]],
    settings: CrossSectionCalibrationSettings,
) -> tuple[list[CreditObservation], list[str]]:
    observations: list[CreditObservation] = []
    warnings: list[str] = []
    rejected_by_reason: dict[str, int] = defaultdict(int)

    requested_tenors = {tenor.upper() for tenor in settings.tenors}
    requested_source = settings.source.upper() if settings.source else None
    prefix = settings.quote_id_prefix.upper() if settings.quote_id_prefix else None

    for quote in quotes:
        quote_id = str(quote.get("canonical_id") or "")
        if quote.get("asset_class") != "CREDIT" or quote.get("quote_type") not in {"SPREAD", "RATE"}:
            continue
        if settings.currency and (quote.get("currency") or "").upper() != settings.currency:
            continue
        if requested_source and str(quote.get("source") or "").upper() != requested_source:
            continue
        if prefix and not quote_id.upper().startswith(prefix):
            continue

        try:
            spread = float(quote["normalized_value"])
        except (KeyError, TypeError, ValueError):
            rejected_by_reason["invalid spread"] += 1
            continue
        if spread <= 0.0:
            rejected_by_reason["non-positive spread"] += 1
            continue

        metadata = _flatten_metadata(quote)
        tenor_value = _metadata_value(metadata, "tenor") or _infer_tenor(quote_id)
        try:
            tenor = normalize_tenor(tenor_value)
        except ValueError:
            rejected_by_reason["missing or invalid tenor"] += 1
            continue
        if requested_tenors and tenor not in requested_tenors:
            continue

        contributors_value = _metadata_value(metadata, "contributors")
        try:
            contributors = (
                int(float(contributors_value))
                if contributors_value not in (None, "")
                else 0
            )
        except (TypeError, ValueError):
            contributors = 0
        if contributors < settings.minimum_contributors:
            rejected_by_reason["below minimum contributors"] += 1
            continue

        try:
            sector = normalize_category(_metadata_value(metadata, "sector"))
            region = normalize_category(_metadata_value(metadata, "region"))
            rating = normalize_rating(_metadata_value(metadata, "rating"))
            seniority = normalize_category(
                _metadata_value(metadata, "seniority") or "SENIOR"
            )
        except ValueError:
            rejected_by_reason["missing cross-section category"] += 1
            continue

        entity_id = str(
            _metadata_value(metadata, "entity_id")
            or quote.get("source_symbol")
            or quote_id
        ).strip()
        weight = _to_positive_float(
            _metadata_value(metadata, "liquidity_weight"),
            float(contributors) if contributors > 0 else 1.0,
        )
        observations.append(
            CreditObservation(
                entity_id=entity_id,
                tenor=tenor,
                time=parse_tenor_years(tenor),
                spread=spread,
                sector=sector,
                region=region,
                rating=rating,
                seniority=seniority,
                weight=weight,
                quote_id=quote_id,
            )
        )

    for reason, count in sorted(rejected_by_reason.items()):
        warnings.append(f"Skipped {count} credit quote(s): {reason}")
    if not observations:
        raise ValueError(
            "No eligible liquid-name credit quotes. Required metadata fields are tenor, "
            "sector, region, rating and seniority; optional contributors/liquidity_weight "
            "control calibration weights."
        )
    return observations, warnings


def _basket(items: list[CreditBasketConstituentRequest]) -> list[BasketConstituent]:
    return [
        BasketConstituent(
            sector=item.sector,
            region=item.region,
            rating=item.rating,
            seniority=item.seniority,
            weight=item.weight,
            name=item.name,
        )
        for item in items
    ]


def _calibrate(
    service: MarketDataService,
    as_of_date: date,
    policy: str,
    settings: CrossSectionCalibrationSettings,
) -> tuple[dict[str, Any], list[Any], list[str], list[CreditObservation]]:
    resolution = _resolve_snapshot(service, as_of_date, policy)
    market_date = resolution["market_data_date"]
    quotes = service.get_quotes(market_date)
    if quotes is None:
        raise HTTPException(
            status_code=404, detail="Resolved market-data snapshot has no quotes"
        )
    try:
        observations, extraction_warnings = _extract_observations(quotes, settings)
        fits = fit_cross_section_curve(
            observations,
            ridge=settings.ridge,
            minimum_observations=settings.minimum_observations_per_tenor,
            requested_tenors=settings.tenors or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    fit_warnings = [warning for fit in fits for warning in fit.warnings]
    warnings = list(dict.fromkeys([*extraction_warnings, *fit_warnings]))
    return resolution, fits, warnings, observations


def _base_response(
    payload: NomuraCrossSectionCalibrationRequest,
    resolution: dict[str, Any],
    fits: list[Any],
    warnings: list[str],
    observations: list[CreditObservation],
) -> dict[str, Any]:
    quote_ids = sorted({observation.quote_id for observation in observations})
    return {
        "as_of_date": payload.as_of_date,
        "market_data_date": resolution["market_data_date"],
        "snapshot_policy": payload.snapshot_policy,
        "exact_snapshot": resolution["exact_snapshot"],
        "snapshot_checksum": resolution["snapshot"]["checksum"],
        "model": {
            "name": "nomura_cross_section_log_ols",
            "equation": "log(spread) = global + sector + region + rating + seniority",
            "dimensions": ["sector", "region", "rating", "seniority"],
            "calibration": (
                "weighted least squares in log spreads with ridge stabilisation"
            ),
            "normalisation": (
                "each dimension is recentered to an observation-weighted "
                "geometric mean factor of 1"
            ),
            "ridge": payload.settings.ridge,
            "minimum_observations_per_tenor": (
                payload.settings.minimum_observations_per_tenor
            ),
            "minimum_contributors": payload.settings.minimum_contributors,
        },
        "tenor_fits": [fit.as_dict() for fit in fits],
        "observations_used": len(observations),
        "quote_ids_used": quote_ids,
        "warnings": warnings,
    }


@router.post("/calibrate", response_model=NomuraCrossSectionResponse)
def calibrate_cross_section(
    payload: NomuraCrossSectionCalibrationRequest, request: Request
) -> dict[str, Any]:
    service = MarketDataService(request.app.state.database)
    resolution, fits, warnings, observations = _calibrate(
        service, payload.as_of_date, payload.snapshot_policy, payload.settings
    )
    return _base_response(payload, resolution, fits, warnings, observations)


@router.post("/basket-curve", response_model=NomuraBasketCurveResponse)
def basket_curve(payload: NomuraBasketCurveRequest, request: Request) -> dict[str, Any]:
    service = MarketDataService(request.app.state.database)
    resolution, fits, warnings, observations = _calibrate(
        service, payload.as_of_date, payload.snapshot_policy, payload.settings
    )
    try:
        curve, node_payloads = build_basket_credit_curve(
            fits,
            _basket(payload.basket),
            recovery_rate=payload.recovery_rate,
            aggregation=payload.aggregation,
            missing_category_policy=payload.missing_category_policy,
            snapshot_checksum=resolution["snapshot"]["checksum"] or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    curve_payload = curve.as_dict()
    if payload.include_constituent_spreads:
        curve_payload["nodes"] = node_payloads
    response = _base_response(payload, resolution, fits, warnings, observations)
    response["curve"] = curve_payload
    return response


@router.post("/xva", response_model=NomuraCrossSectionXvaResponse)
def credit_xva(payload: NomuraCrossSectionXvaRequest, request: Request) -> dict[str, Any]:
    service = MarketDataService(request.app.state.database)
    resolution, fits, warnings, observations = _calibrate(
        service, payload.as_of_date, payload.snapshot_policy, payload.settings
    )
    try:
        counterparty_curve, _ = build_basket_credit_curve(
            fits,
            _basket(payload.counterparty_basket),
            recovery_rate=payload.counterparty_recovery_rate,
            aggregation=payload.aggregation,
            missing_category_policy=payload.missing_category_policy,
            snapshot_checksum=resolution["snapshot"]["checksum"] or "",
        )
        own_curve = None
        if payload.own_basket:
            own_curve, _ = build_basket_credit_curve(
                fits,
                _basket(payload.own_basket),
                recovery_rate=payload.own_recovery_rate,
                aggregation=payload.aggregation,
                missing_category_policy=payload.missing_category_policy,
                snapshot_checksum=resolution["snapshot"]["checksum"] or "",
            )
        credit_metrics = calculate_credit_xva(
            [point.model_dump() for point in payload.exposure_profile],
            counterparty_curve,
            own_curve=own_curve,
            discount_rate=payload.discount_rate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    response = _base_response(payload, resolution, fits, warnings, observations)
    response.update(
        {
            "counterparty_curve": counterparty_curve.as_dict(),
            "own_curve": own_curve.as_dict() if own_curve else None,
            "credit_xva": credit_metrics,
        }
    )
    return response
