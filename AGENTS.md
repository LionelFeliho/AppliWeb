# Project instructions for coding agents

## Scope

This project is an end-of-day pricing and XVA platform. Correctness, reproducibility and auditability take priority over feature count.

## Architecture

- Keep vendor adapters outside pricing engines.
- Pricing code may consume only canonical market snapshots.
- Keep FastAPI routers thin; business logic belongs in services or pure quant modules.
- Do not place pricing logic in the JavaScript frontend.
- Preserve raw source values, conversion rules and normalized values.
- Every calculation must include an as-of date, resolved market-data date and snapshot checksum.
- XVA calculations will operate at netting-set and CSA level, not merely trade level.

## Numerical standards

- Use `Decimal` for persisted market data and money-like inputs; document deliberate float use in simulation engines.
- Do not round intermediate calculations.
- Document units, compounding, day-counts, calendars and business-day conventions.
- Never hide a fallback to zero, yesterday's quote, a missing curve node or a default volatility.
- Stale market data must be explicit and measurable.
- Monte Carlo tests must use deterministic seeds and report assumptions and statistical error.
- Add benchmark tests for every pricing model.

## XCCY and curve standards

- FX spot is quote-currency units per one base-currency unit.
- Return all canonical quote IDs used to construct curves and FX crosses.
- Curve completeness failures must be explicit; do not silently extrapolate a missing required pillar.
- Discounting/collateral currency must be an explicit request field and result attribute.
- Cross-gamma diagnostics must state bump sizes and finite-difference convention.
- Exposure and XVA results must return the simulation seed, path count, volatility/correlation source and model warnings.
- Demo market data must be labelled synthetic and must never be represented as an approved close.

## Market-data standards

- Canonical IDs are stable API contracts; rename only through an explicit migration.
- Vendor symbols belong in `source_symbol`, never in pricing code.
- A changed EOD value must require explicit replacement.
- New provider adapters need parser tests using recorded or synthetic fixtures.
- Never commit licensed vendor data or credentials.

## Development

- Python 3.12+, type annotations and descriptive errors.
- Add or update tests for every behavior change.
- Run `pytest -q`, `python -m compileall -q app tests` and `node --check ../web/app.js` before completion.
- Keep API schemas backward compatible unless the change is versioned.
- Do not broaden CORS or disable API-key checks to make a demo work.
