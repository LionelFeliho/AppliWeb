from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from .providers import CsvSettlementProvider, EcbSdmxProvider
from .service import MarketDataService


def _json(value: Any) -> str:
    return json.dumps(value, default=str, indent=2, sort_keys=True)


def _database(url: str | None) -> tuple[Database, Settings]:
    settings = Settings.from_env()
    database = Database(url or settings.database_url)
    database.create_schema()
    return database, settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="XVA EOD market-data utility")
    parser.add_argument("--database-url", help="Override XVA_DATABASE_URL")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create database tables")

    csv_parser = sub.add_parser("ingest-csv", help="Import a settlement CSV")
    csv_parser.add_argument("--date", required=True, type=date.fromisoformat)
    csv_parser.add_argument("--file", required=True, type=Path)
    csv_parser.add_argument("--replace", action="store_true")

    ecb_parser = sub.add_parser("ingest-ecb", help="Retrieve ECB EOD series")
    ecb_parser.add_argument("--date", required=True, type=date.fromisoformat)
    ecb_parser.add_argument("--replace", action="store_true")

    show_parser = sub.add_parser("show", help="Show a snapshot and its quotes")
    show_parser.add_argument("--date", required=True, type=date.fromisoformat)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database, settings = _database(args.database_url)
    service = MarketDataService(database)

    if args.command == "init-db":
        print(_json({"status": "ok", "database_url": database.url}))
        return 0

    if args.command == "ingest-csv":
        provider = CsvSettlementProvider()
        quotes = provider.parse_file(args.file, args.date)
        result = service.ingest_quotes(
            provider.name, args.date, quotes, replace=args.replace
        )
        print(_json(result.as_dict()))
        return 0

    if args.command == "ingest-ecb":
        provider = EcbSdmxProvider(
            base_url=settings.ecb_base_url,
            timeout_seconds=settings.ecb_timeout_seconds,
            lookback_days=settings.ecb_lookback_days,
            max_staleness_days=settings.ecb_max_staleness_days,
        )
        result = service.ingest_quotes(
            provider.name,
            args.date,
            provider.fetch(args.date),
            replace=args.replace,
        )
        print(_json(result.as_dict()))
        return 0

    snapshot = service.get_snapshot(args.date)
    quotes = service.get_quotes(args.date)
    if snapshot is None or quotes is None:
        print(_json({"error": "snapshot not found", "valuation_date": args.date}))
        return 1
    print(_json({"snapshot": snapshot, "quotes": quotes}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
