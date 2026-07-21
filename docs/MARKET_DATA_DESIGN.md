# EOD market-data design

## Invariants

1. A snapshot is identified by an ISO valuation date.
2. A canonical quote ID appears at most once in a snapshot.
3. Source payloads are normalized at the adapter boundary.
4. Raw and normalized values are both persisted.
5. A changed value cannot overwrite an existing quote unless the caller explicitly requests replacement.
6. The snapshot checksum covers every persisted quote field, including raw and normalized values, units, conversion parameters, source identifiers and metadata.
7. Pricing results must retain the snapshot checksum used for the calculation.

## Canonical ID examples

```text
FX.EURUSD.SPOT
RATE.EUR.ESTR.ON
RATE.USD.SOFR.3M.SEP2026
FI.USD.TSY.10Y.YIELD
CR.CDX.NA.IG.46.5Y
EQ.SPX.SPOT
COMMO.COPPER.CME.SEP2026
```

Vendor identifiers are not canonical identifiers. A Bloomberg, LSEG, CME or ICE symbol can change without changing the pricing contract.

## Snapshot lifecycle

```text
BUILDING ── successful validation/commit ──> READY
```

A failed ingestion run is recorded separately. The snapshot transaction is rolled back, so a partially imported batch is never presented as ready.

## Settlement conversion examples

- SOFR future settlement `95.25` → `(100 - 95.25) / 100 = 0.0475`.
- CDS settlement spread `85 bps` → `0.0085`.
- Copper `548.25 cents/lb` → `5.4825 USD/lb`.
- A percent yield `4.38%` → `0.0438`.

Instrument-specific transformations such as bond clean-to-dirty conversion, CTD conversion factors, bootstrap equations or volatility smile construction do **not** belong in this generic unit converter. They belong in dedicated market-building services with explicit conventions.

## Provider hierarchy

A future production configuration should define source priority by canonical quote, for example:

1. approved vendor settlement;
2. exchange settlement file;
3. official administrator publication;
4. controlled manual override.

Manual overrides need maker-checker approval, reason, author and expiry. The current MVP intentionally does not implement silent fallback or manual override.

## Historical as-of resolution

The API supports two explicit policies:

- `exact`: resolve only a `READY` snapshot whose `valuation_date` equals the requested as-of date;
- `previous_ready`: resolve the maximum `READY valuation_date <= as_of_date`.

The resolver is constrained by `valuation_date <= as_of_date`, so it cannot introduce look-ahead bias. Responses and pricing results expose both `as_of_date` and `market_data_date` plus the selected snapshot checksum.

A weekend or holiday fallback is therefore visible and reproducible rather than implicit.
