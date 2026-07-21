from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .db import Database
from .market_data.domain import (
    MarketDataConflictError,
    MarketDataValidationError,
    ProviderError,
)
from .market_data.routes import router as market_data_router
from .pricing import router as pricing_router
from .xccy_routes import market_router as g10_market_router, pricing_router as xccy_pricing_router, reference_router


def create_app(
    database_url: str | None = None,
    admin_api_key: str | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    base_settings = settings or Settings.from_env()
    if database_url is not None or admin_api_key is not None:
        base_settings = Settings(
            database_url=database_url or base_settings.database_url,
            admin_api_key=(
                admin_api_key if admin_api_key is not None else base_settings.admin_api_key
            ),
            cors_origins=base_settings.cors_origins,
            log_level=base_settings.log_level,
            ecb_base_url=base_settings.ecb_base_url,
            ecb_timeout_seconds=base_settings.ecb_timeout_seconds,
            ecb_lookback_days=base_settings.ecb_lookback_days,
            ecb_max_staleness_days=base_settings.ecb_max_staleness_days,
        )

    logging.basicConfig(level=getattr(logging, base_settings.log_level, logging.INFO))
    database = Database(base_settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.create_schema()
        yield

    app = FastAPI(
        title="XVA EOD Pricing API",
        version=__version__,
        description=(
            "Auditable end-of-day market-data snapshots, G10 cross-currency pricing, "
            "exposure profiles and first-principles XVA diagnostics."
        ),
        lifespan=lifespan,
    )
    app.state.settings = base_settings
    app.state.database = database

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(base_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key"],
    )

    @app.exception_handler(MarketDataValidationError)
    async def validation_error_handler(
        request: Request, exc: MarketDataValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors})

    @app.exception_handler(MarketDataConflictError)
    async def conflict_error_handler(
        request: Request, exc: MarketDataConflictError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ProviderError)
    async def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get("/health", tags=["Operations"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "mode": "EOD"}

    app.include_router(market_data_router)
    app.include_router(pricing_router)
    app.include_router(xccy_pricing_router)
    app.include_router(reference_router)
    app.include_router(g10_market_router)
    return app


app = create_app()
