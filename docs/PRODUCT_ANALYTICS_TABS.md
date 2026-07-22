# Product analytics tab standard

Every new priced product should expose a consistent result workspace rather than a product-specific wall of metrics.

## Required tabs

### Overview

- clean PV and reporting currency;
- alternative pricing or discounting regimes;
- valuation adjustments and adjusted PV;
- the most material headline risk measures.

### Exposure

- deterministic forward MTM where applicable;
- EPE, ENE and PFE for simulated products;
- collateral or margin profile when available.

### Sensitivities

- market-risk summary measures;
- bucketed term structures with canonical quote lineage;
- bump sizes and normalization conventions;
- first- and second-order measures when numerically stable;
- XVA spread sensitivities clearly separated from clean-PV sensitivities.

### Market & controls

- as-of date and resolved market-data date;
- snapshot checksum;
- curve/surface nodes and canonical quote IDs;
- model assumptions, fallbacks and validation warnings.

## API convention

A product should expose an auditable sensitivity analysis either in the main response or, preferably for expensive calculations, through an on-demand endpoint such as:

```text
POST /api/v1/pricing/<product>/sensitivities
```

The sensitivity response should contain:

```text
methodology
reporting_currency
bump definitions
bucketed term structures
summary sensitivities
XVA or valuation-adjustment sensitivities, when applicable
```

Bucket records should contain enough information to reproduce and audit the number: risk-factor role, currency, tenor or expiry, time, quote ID, market level, up/down P&L, normalized delta and normalized gamma.

A sensitivity tab must never hide whether results are clean-PV bump-and-reprice, frozen-exposure XVA sensitivities or a full re-simulation. The UI should lazy-load expensive sensitivity calculations and clearly identify the trade request and market snapshot used.
