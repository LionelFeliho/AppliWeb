# XVA EOD Pricer

Standalone API and web application for **end-of-day market data, historical pricing and the future multi-asset XVA stack**.

This repository is intentionally independent from any SIMM implementation. Pricing engines consume canonical, dated market snapshots; they do not import market data or logic from an unrelated margin repository.

The project is currently hosted in the temporary GitHub repository `LionelFeliho/AppliWeb`. The repository can be renamed later without changing the application architecture.

## Current vertical slice

- FastAPI backend with Swagger/OpenAPI.
- JavaScript dashboard calling the backend.
- PostgreSQL storage with one persistent snapshot per EOD date.
- SQLite fallback for tests and lightweight local development.
- Generic settlement CSV importer.
- ECB SDMX connector for EUR FX reference rates and €STR.
- Audit trail containing raw value, normalized value, source symbol, conversion rule, source observation date and metadata.
- Deterministic SHA-256 checksum for every complete snapshot.
- Explicit as-of-date resolution using either `exact` or `previous_ready`.
- Diagnostic zero-coupon pricer consuming a resolved historical snapshot.
- GitHub Actions CI and scheduled EOD ingestion workflow.
- GitHub Codespaces / VS Code Cloud development environment.

## Architecture

```text
apps/web
   │ HTTP / JSON
   ▼
apps/api (FastAPI / Swagger)
   │
   ├── EOD ingestion
   │     ├── settlement CSV
   │     └── ECB SDMX
   ├── canonical normalization
   ├── historical snapshot resolution
   ├── first pricing vertical slice
   └── PostgreSQL
```

Future rates, fixed-income, FX, credit, equity, commodity and XVA engines must depend only on the canonical snapshot layer.

## Historical database model

```text
market_data_snapshots
├── valuation_date        unique EOD date
├── status                BUILDING / READY
├── quote_count
├── checksum
└── timestamps

market_quotes
├── snapshot_id
├── canonical_id          unique inside one snapshot
├── raw_value
├── normalized_value
├── conversion
├── source / source_symbol
├── metadata
└── timestamps
```

The PostgreSQL volume is named `xva_pgdata`. A daily job adds or completes the snapshot for that date. Older dates remain queryable.

A changed quote on an existing date is never overwritten silently. It requires `replace=true`.

## As-of-date policies

Every historical query makes its resolution policy explicit:

- `exact`: a `READY` snapshot must exist on the requested date;
- `previous_ready`: use the latest `READY` snapshot on or before the requested date.

`previous_ready` is useful for weekends and holidays. It never looks forward and responses always expose both dates:

```json
{
  "as_of_date": "2026-07-19",
  "market_data_date": "2026-07-17",
  "snapshot_policy": "previous_ready",
  "exact_snapshot": false
}
```

## GitHub Codespaces / VS Code Cloud

On GitHub, select **Code → Codespaces → Create codespace on main**.

The Dev Container:

- opens this repository directly as the workspace;
- installs Python 3.12 and Node 22 tooling;
- installs backend dependencies;
- starts PostgreSQL;
- forwards ports `8000`, `3000` and `5432`;
- installs Python, Pylance, Debugpy, Docker, YAML, GitLens and GitHub Actions extensions.

Useful VS Code actions:

- `F5` with **XVA API (FastAPI)** starts the API debugger;
- **Terminal → Run Task → XVA: Start full Docker stack** starts API, web and database;
- **XVA: Backend tests** runs pytest;
- **XVA: Import sample EOD** loads the sample snapshot.

## Current GitHub repository

The source is currently published in:

```text
LionelFeliho/AppliWeb
```

It is a standalone repository and has no code dependency on `ISDA_SIMM`. Renaming the GitHub repository later will preserve GitHub redirects; local clones may update their remote URL after the rename.

## Quick start with Docker

```bash
cp .env.example .env
docker compose up --build
```

Open:

- dashboard: `http://localhost:3000`;
- Swagger: `http://localhost:8000/docs`;
- health: `http://localhost:8000/health`;
- PostgreSQL: `localhost:5432`.

The development API key is empty by default. Set `XVA_ADMIN_API_KEY` before exposing mutation endpoints outside a trusted environment.

## Local backend without Docker

```bash
cd apps/api
python -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Run the EOD loader

From `apps/api`:

```bash
python ../../scripts/run_eod.py \
  --date 2026-07-20 \
  --settlement-file ../../sample_data/settlements.csv
```

Offline sample:

```bash
python ../../scripts/run_eod.py \
  --date 2026-07-20 \
  --skip-ecb \
  --settlement-file ../../sample_data/settlements.csv
```

The loader creates or reuses that date's snapshot, imports data, normalizes quotes, calculates the checksum and prints the resulting inventory.

## API examples

### List stored dates

```bash
curl "http://localhost:8000/api/v1/market-data/snapshots?limit=3650"
```

### Resolve an exact date

```bash
curl "http://localhost:8000/api/v1/market-data/as-of/2026-07-20?policy=exact"
```

### Resolve a weekend to the previous EOD

```bash
curl "http://localhost:8000/api/v1/market-data/as-of/2026-07-19/quotes?policy=previous_ready"
```

### Import settlement prices

```bash
curl -X POST \
  "http://localhost:8000/api/v1/market-data/ingestions/settlements?valuation_date=2026-07-20" \
  -H "Content-Type: text/csv" \
  -H "X-API-Key: ${XVA_ADMIN_API_KEY}" \
  --data-binary @sample_data/settlements.csv
```

### Retrieve ECB data

```bash
curl -X POST \
  "http://localhost:8000/api/v1/market-data/ingestions/ecb?valuation_date=2026-07-20" \
  -H "X-API-Key: ${XVA_ADMIN_API_KEY}"
```

### Historical zero-coupon pricing

```bash
curl -X POST "http://localhost:8000/api/v1/pricing/zero-coupon" \
  -H "Content-Type: application/json" \
  -d '{
    "as_of_date": "2026-07-19",
    "maturity_date": "2027-07-19",
    "notional": 1000000,
    "currency": "EUR",
    "discount_rate_quote_id": "RATE.EUR.ESTR.ON",
    "compounding": "continuous",
    "snapshot_policy": "previous_ready"
  }'
```

The response includes the requested as-of date, the actual market-data date and the snapshot checksum.

The legacy request field `valuation_date` is accepted as an input alias during the API transition, but new clients should send `as_of_date`.

## Settlement CSV contract

Required columns:

| Column | Meaning |
|---|---|
| `canonical_id` | Stable identifier consumed by pricing engines. |
| `asset_class` | `RATES`, `FIXED_INCOME`, `FX`, `CREDIT`, `EQUITY`, or `COMMODITY`. |
| `quote_type` | `SPOT`, `RATE`, `YIELD`, `PRICE`, `SPREAD`, `VOLATILITY`, `FORWARD`, or `DISCOUNT_FACTOR`. |
| `raw_value` | Vendor or exchange value. `settlement_price` is accepted as an alias. |

Recommended columns:

| Column | Meaning |
|---|---|
| `valuation_date` | ISO date; blank inherits the requested ingestion date. |
| `source` | Vendor, exchange or administrator. |
| `source_symbol` | Original vendor ticker or contract code. |
| `conversion` | Conversion rule; defaults to `identity`. |
| `factor` / `shift` | Final linear adjustment. |
| `currency` | Quote currency. |
| `unit` | Normalized unit. |
| `metadata_json` | Optional JSON retained for audit. |

Example:

```csv
valuation_date,source,source_symbol,canonical_id,asset_class,quote_type,raw_value,conversion,factor,shift,currency,unit
2026-07-20,CME,SR3U6,RATE.USD.SOFR.3M.SEP2026,RATES,RATE,95.25,futures_price_to_rate,1,0,USD,DECIMAL
2026-07-20,ICE,CDX.NA.IG.46,CR.CDX.NA.IG.46.5Y,CREDIT,SPREAD,85,bps_to_decimal,1,0,USD,DECIMAL
2026-07-20,CME,HGU6,COMMO.COPPER.CME.SEP2026,COMMODITY,PRICE,548.25,cents_to_unit,1,0,USD,USD_PER_LB
```

## Conversion rules

| Rule | Base formula |
|---|---|
| `identity` / `linear` | `raw` |
| `percent_to_decimal` | `raw / 100` |
| `bps_to_decimal` | `raw / 10,000` |
| `futures_price_to_rate` | `(100 - raw) / 100` |
| `inverse` | `1 / raw` |
| `fx_pips` | `raw / 10,000` |
| `cents_to_unit` | `raw / 100` |

The final normalized value is `base_conversion * factor + shift`.

Instrument-specific transformations such as clean-to-dirty bond conversion, futures CTD conversion, curve bootstrap or volatility construction belong in dedicated market-building services.

## Scheduled ingestion

`.github/workflows/eod-ingestion.yml` runs on weekdays at 18:15 UTC and may also be triggered manually.

It requires repository secrets:

```text
XVA_API_BASE_URL
XVA_ADMIN_API_KEY
```

The workflow calls the deployed API. A Codespace is a development environment, not the production data store; long-term history should use a persistent deployed PostgreSQL service.

## Tests

```bash
make test
```

or:

```bash
cd apps/api
python -m pytest -q
```

The tests cover ingestion protection, idempotency, explicit replacement, checksum integrity, exact and previous-ready resolution, no-look-ahead behavior, historical pricing, currency validation and mocked ECB parsing.

## Guardrails

- Every pricing request names an as-of date.
- Every result exposes the market-data date actually used.
- Quotes are unique by snapshot and canonical ID.
- Re-ingestion is idempotent.
- Corrections require `replace=true`.
- Snapshot checksums cover every persisted quote field and metadata.
- Raw and normalized values are retained together.
- No silent fallback to zero, today, a future date or an unversioned “latest” quote.
- Secrets and licensed vendor data must not be committed.

## Next implementation sequence

1. Instrument master, calendars, day-counts and conventions.
2. OIS/IBOR curve bootstrap and completeness controls.
3. Deposits, FRAs, OIS, swaps and fixed-rate bonds.
4. FX forwards/options, credit curves/CDS, equity and commodity forwards/options.
5. Portfolio, counterparty, netting-set and CSA models.
6. Scenario generation and exposure profiles.
7. CVA, DVA, FVA and COLVA, followed by MVA and KVA.
8. Independent benchmarks against QuantLib, ORE or approved vendor outputs.
