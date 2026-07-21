# Project instructions for coding agents

## Scope

This project is an end-of-day pricing and XVA platform. Correctness, reproducibility and auditability take priority over feature count.

## Architecture

- Keep vendor adapters outside pricing engines.
- Pricing code may consume only canonical market snapshots.
- Keep FastAPI routers thin; business logic belongs in services.
- Do not place pricing logic in the JavaScript frontend.
- Preserve raw source values, conversion rules and normalized values.
- Every calculation must include a valuation date and market snapshot checksum.
- XVA calculations will operate at netting-set and CSA level, not merely trade level.

## Numerical standards

- Use `Decimal` for persisted market data and money-like inputs.
- Do not round intermediate calculations.
- Document units, compounding, day-count, calendars and business-day conventions.
- Never hide a fallback to zero, yesterday's quote, or a default volatility.
- Stale market data must be explicit and measurable.
- Monte Carlo tests must use deterministic seeds and report statistical error.
- Add benchmark tests for every pricing model.

## Market-data standards

- Canonical IDs are stable API contracts; rename only through an explicit migration.
- Vendor symbols belong in `source_symbol`, never in pricing code.
- A changed EOD value must require explicit replacement.
- New provider adapters need parser tests using recorded or synthetic fixtures.
- Never commit licensed vendor data or credentials.

## Development

- Python 3.12+, type annotations and descriptive errors.
- Add or update tests for every behavior change.
- Run `pytest -q` before completion.
- Keep API schemas backward compatible unless the change is versioned.
- Do not broaden CORS or disable API-key checks to make a demo work.
