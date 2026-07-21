from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.market_data.providers import CsvSettlementProvider, EcbSdmxProvider  # noqa: E402
from app.market_data.service import MarketDataService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the XVA EOD market-data load")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--settlement-file", type=Path)
    parser.add_argument("--skip-ecb", action="store_true")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--database-url")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings.from_env()
    database = Database(args.database_url or settings.database_url)
    database.create_schema()
    service = MarketDataService(database)
    runs: list[dict[str, object]] = []

    if not args.skip_ecb:
        ecb = EcbSdmxProvider(
            base_url=settings.ecb_base_url,
            timeout_seconds=settings.ecb_timeout_seconds,
            lookback_days=settings.ecb_lookback_days,
            max_staleness_days=settings.ecb_max_staleness_days,
        )
        runs.append(
            service.ingest_quotes(
                ecb.name, args.date, ecb.fetch(args.date), replace=args.replace
            ).as_dict()
        )

    if args.settlement_file:
        csv_provider = CsvSettlementProvider()
        runs.append(
            service.ingest_quotes(
                csv_provider.name,
                args.date,
                csv_provider.parse_file(args.settlement_file, args.date),
                replace=args.replace,
            ).as_dict()
        )

    if not runs:
        raise SystemExit("Nothing to ingest: provide --settlement-file or remove --skip-ecb")

    output = {
        "runs": runs,
        "snapshot": service.get_snapshot(args.date),
        "quotes": service.get_quotes(args.date),
    }
    print(json.dumps(output, default=str, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
