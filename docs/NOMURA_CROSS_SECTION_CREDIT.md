# Nomura-style cross-section proxy credit curves

## Purpose

This module constructs transparent proxy credit spread curves for counterparties or weighted baskets when liquid single-name CDS curves are unavailable. It implements the multiplicative cross-section model described in Nomura's 2013 paper *A cross-section across CVA*:

```text
proxy spread = global × sector × region × rating × seniority
```

Taking logarithms turns the calibration into a linear cross-sectional regression at each tenor:

```text
log(spread_i,T) = alpha_T
                + beta_sector(i),T
                + beta_region(i),T
                + beta_rating(i),T
                + beta_seniority(i),T
                + error_i,T
```

The implementation uses weighted least squares in log spreads with a small configurable ridge term. After calibration, each dimension is re-centred so its observation-weighted geometric mean factor is one; the intercept is therefore the global spread factor and fitted spreads are unchanged.

## Market-data contract

Liquid credit quotes are ordinary canonical market-data rows with:

- `asset_class=CREDIT`;
- `quote_type=SPREAD` or `RATE`;
- a positive normalized decimal spread;
- tenor and cross-section attributes in CSV columns or `metadata_json`.

Supported metadata aliases include:

| Required concept | Accepted examples |
|---|---|
| entity | `entity_id`, `obligor`, `issuer`, `reference_entity`, `ticker` |
| tenor | `tenor`, `maturity`, `cds_tenor`, or a tenor embedded in the canonical ID |
| sector | `sector`, `industry`, `industry_sector` |
| region | `region`, `geography`, `country_region` |
| rating | `rating`, `credit_rating`, `composite_rating` |
| seniority | `seniority`, `debt_seniority`, `tier` |
| liquidity | `contributors`, `contributor_count`, `liquidity_weight`, `weight` |

The generic settlement importer retains unknown columns under `metadata.source_columns`, so no vendor-specific field is required in pricing code.

## API

### Calibrate factor curves

```http
POST /api/v1/credit/nomura-cross-section/calibrate
```

### Build a weighted basket proxy curve

```http
POST /api/v1/credit/nomura-cross-section/basket-curve
```

Example:

```json
{
  "as_of_date": "2026-07-20",
  "snapshot_policy": "exact",
  "settings": {
    "currency": "USD",
    "tenors": ["1Y", "3Y", "5Y", "7Y", "10Y"],
    "minimum_observations_per_tenor": 12,
    "minimum_contributors": 3
  },
  "basket": [
    {
      "name": "European BBB financial senior",
      "sector": "FINANCIALS",
      "region": "EUROPE",
      "rating": "BBB",
      "seniority": "SENIOR",
      "weight": 0.75
    },
    {
      "name": "North American A industrial senior",
      "sector": "INDUSTRIALS",
      "region": "NORTH_AMERICA",
      "rating": "A",
      "seniority": "SENIOR",
      "weight": 0.25
    }
  ],
  "recovery_rate": 0.4,
  "aggregation": "arithmetic"
}
```

The response includes the global and category factor curves, calibration diagnostics, every market quote ID used, basket proxy spreads, hazard rates, survival probabilities and cumulative default probabilities.

### Use the proxy curve in XVA

```http
POST /api/v1/credit/nomura-cross-section/xva
```

Supply an EPE/ENE profile together with counterparty and optional own-credit baskets. The endpoint calibrates the same historical snapshot, builds the curves and integrates interval CVA/DVA contributions.

## Basket aggregation

Two spread aggregations are returned at every tenor:

- weighted arithmetic spread, suitable as a simple expected-loss basket proxy;
- weighted geometric spread, consistent with the log-linear calibration.

The selected aggregation is converted to a piecewise-constant hazard rate through:

```text
hazard = spread / (1 - recovery)
```

This transparent approximation is intentional for the first XVA integration. It is not a full CDS premium/protection-leg bootstrap. A production curve builder should add accrued premium, discounting, standard CDS schedules, restructuring clauses and calibrated recovery assumptions.

## Controls and limitations

- Quotes with missing tenor, sector, region, rating or seniority are excluded and counted in warnings.
- Minimum contributor and source filters are explicit.
- Unseen basket categories fail by default; `missing_category_policy=global` is an explicit fallback.
- The model is calibrated independently by tenor.
- Sparse categories, rank deficiency and ill-conditioning are returned as diagnostics.
- The current model does not enforce rating monotonicity; this should be a separately governed post-calibration option rather than a hidden adjustment.
- Synthetic demo data is labelled `DEMO_SYNTHETIC` and must not be used as a production close.
