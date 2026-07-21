from __future__ import annotations

import os
from dataclasses import dataclass


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_api_key: str
    cors_origins: tuple[str, ...]
    log_level: str
    ecb_base_url: str
    ecb_timeout_seconds: int
    ecb_lookback_days: int
    ecb_max_staleness_days: int

    @classmethod
    def from_env(cls) -> "Settings":
        origins = tuple(
            item.strip()
            for item in os.getenv("XVA_CORS_ORIGINS", "http://localhost:3000").split(",")
            if item.strip()
        )
        return cls(
            database_url=os.getenv("XVA_DATABASE_URL", "sqlite:///./data/xva_eod.db"),
            admin_api_key=os.getenv("XVA_ADMIN_API_KEY", ""),
            cors_origins=origins,
            log_level=os.getenv("XVA_LOG_LEVEL", "INFO").upper(),
            ecb_base_url=os.getenv(
                "XVA_ECB_BASE_URL", "https://data-api.ecb.europa.eu/service"
            ).rstrip("/"),
            ecb_timeout_seconds=_int_env("XVA_ECB_TIMEOUT_SECONDS", 30),
            ecb_lookback_days=_int_env("XVA_ECB_LOOKBACK_DAYS", 7),
            ecb_max_staleness_days=_int_env("XVA_ECB_MAX_STALENESS_DAYS", 7),
        )
