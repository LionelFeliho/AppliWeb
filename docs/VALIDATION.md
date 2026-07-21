# Validation plan

## Automated invariants currently enforced

- repeated EOD ingestion is idempotent;
- changed quotes require explicit replacement;
- snapshot checksums change when any persisted quote field changes;
- `previous_ready` never selects future data;
- G10 curve readiness requires core OIS tenors;
- inverse and triangulated FX paths are deterministic;
- zero-basis discounting representations agree under covered interest parity;
- EPE is non-negative and ENE is non-positive;
- identical simulation seeds produce identical profiles;
- maturity forward MTM is zero after all cash flows have settled;
- PV after XVA equals PV plus total XVA.

## Independent benchmark sequence

1. Compare discount factors and forwards with QuantLib `ZeroCurve` under identical interpolation.
2. Compare fixed/floating leg PVs with QuantLib cash-flow schedules using matched day counts.
3. Compare zero-basis XCCY representation equivalence.
4. Compare basis-shift PV and DV01 with finite-difference benchmark workbooks.
5. Reproduce an ORE XCCY trade after the full instrument bootstrap and calendar layer are implemented.
6. Compare exposure percentiles with an independently seeded Monte Carlo implementation.
7. Validate CVA integration against a deterministic exposure/default-probability spreadsheet.

No model should be promoted for production valuation until benchmark tolerances, market conventions and governance approvals are documented.
