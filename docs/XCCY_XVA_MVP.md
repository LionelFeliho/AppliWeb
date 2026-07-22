# G10 XCCY and XVA vertical slice

## Scope

This slice prices a constant-notional, floating/floating cross-currency swap from an auditable EOD snapshot. It is intended to establish the API contract, market-data controls, discounting comparison, exposure graphs and initial XVA plumbing before full model validation.

Supported currencies are USD, EUR, GBP, JPY, CHF, CAD, AUD, NZD, NOK and SEK.

## Required closing market data

Each currency curve uses the canonical OIS zero-rate nodes:

```text
RATE.<CCY>.OIS.1M
RATE.<CCY>.OIS.3M
RATE.<CCY>.OIS.6M
RATE.<CCY>.OIS.1Y
RATE.<CCY>.OIS.2Y
RATE.<CCY>.OIS.5Y
RATE.<CCY>.OIS.10Y
```

Strict completeness is enabled by default. A missing required node causes a `422` response; the pricer does not silently flatten, carry forward or substitute another quote.

FX spot is interpreted as **quote-currency units per one base-currency unit**. The resolver tries, in order:

1. `FX.<BASE><QUOTE>.SPOT`;
2. inverse `FX.<QUOTE><BASE>.SPOT`;
3. a USD cross using direct or inverse USD pillars.

The response lists every quote ID used and the resolution path.

`sample_data/g10_xccy_demo.csv` contains synthetic data for functional testing only. It is not an approved valuation close.

## Curves and cash flows

The current curve is a continuously compounded zero curve with log-linear interpolation of discount factors. One OIS curve per currency is used for both projection and discounting in this MVP.

Floating coupons are projected from adjacent discount factors:

```text
F(t0,t1) = (DF(t0) / DF(t1) - 1) / accrual
```

The schedule supports ACT/360, ACT/365F and 30E/360. Currency convention names and OIS indices are registered. Until audited holiday files are installed, business-day adjustment applies weekends only.

## Discounting switch

Let `S` be quote currency per base currency. The cross-currency forward is:

```text
F(0,T) = S × DF_base(T) / DF_quote(T) × exp(basis × T)
```

The API returns three PVs:

- `native`: each leg discounted on its own OIS curve, then converted at spot;
- `base_collateral`: quote cash flows converted through forwards and discounted on the base curve;
- `quote_collateral`: base cash flows converted through forwards and discounted on the quote curve.

With zero basis and internally consistent curves, the three representations reconcile numerically. A non-zero basis produces a collateral/discounting switch impact.

## FX × discount cross-gamma diagnostic

The response reports the interaction P&L for a +1% FX spot bump and a +1 bp parallel curve bump:

```text
Interaction = PV(FX+, rate+) - PV(FX+, rate0) - PV(FX0, rate+) + PV(FX0, rate0)
```

The diagnostic is calculated separately for the base and quote discount curves. A normalized mixed finite-difference derivative is also returned. This is a sensitivity diagnostic, not a regulatory or accounting definition of gamma.

## Exposure model

The exposure engine uses payment dates as the time grid and simulates:

- lognormal FX around the current cross-currency forward;
- Gaussian parallel base-curve shifts;
- Gaussian parallel quote-curve shifts;
- user-supplied correlations and a deterministic seed.

For each date it returns:

- deterministic forward MTM;
- EPE;
- ENE, represented as a negative value for charting;
- PFE at the requested quantile;
- expected absolute collateral balance.

Collateral is currently an instantaneous threshold and minimum-transfer-amount approximation. Margin call frequency, settlement lag, disputes, independent amount and margin period of risk are not yet modelled.

## XVA metrics

The first metrics are:

```text
CVA   = LGD_counterparty × discounted EPE × incremental default probability
DVA   = LGD_own × discounted |ENE| × incremental own default probability
FVA   = funding spread × discounted EPE × dt
COLVA = collateral spread × discounted expected collateral × dt
```

Flat hazards are inferred from spread divided by LGD. The adjusted PV is:

```text
clean PV - CVA + DVA - FVA - COLVA
```

MVA and KVA are not yet included.

## API

```http
POST /api/v1/pricing/xccy-swap
POST /api/v1/pricing/xccy-swap/sensitivities
GET  /api/v1/reference/g10
```

Minimal request using default canonical OIS nodes:

```json
{
  "as_of_date": "2026-07-20",
  "maturity_date": "2031-07-20",
  "base_currency": "EUR",
  "quote_currency": "USD",
  "notional_base": 10000000,
  "pay_base": true,
  "quote_spread_bps": 10,
  "cross_currency_basis_bps": 25,
  "discounting_mode": "base_collateral",
  "compare_discounting": true,
  "simulation": {
    "paths": 512,
    "seed": 42,
    "fx_volatility": 0.10,
    "collateral_threshold": 0,
    "minimum_transfer_amount": 0
  },
  "xva": {
    "counterparty_spread_bps": 100,
    "own_spread_bps": 80,
    "funding_spread_bps": 50,
    "collateral_spread_bps": 0
  },
  "sensitivities": {
    "rate_bump_bps": 1,
    "fx_bump_relative": 0.01,
    "basis_bump_bps": 1,
    "spread_bump_bps": 1
  }
}
```

## Product analytics tabs and sensitivity term structure

After a successful XCCY calculation the dashboard exposes four reusable product tabs:

1. **Overview** — clean PV, XVA-adjusted PV, valuation adjustments, crossed gamma and discounting comparison;
2. **Exposure** — forward MTM, EPE, ENE and PFE term structures;
3. **Sensitivities** — curve-node PV01, FX delta/gamma, XCCY-basis PV01 and XVA spread 01s;
4. **Market & controls** — curve quote lineage, zero rates, discount factors and modelling warnings.

The Sensitivities tab lazy-loads the dedicated endpoint. This avoids recalculating a full bucketed risk report for users who only need the headline PV or exposure profile.

The clean-PV sensitivity engine uses central bump-and-reprice. For a curve bucket with a configurable bump `b` expressed in basis points:

```text
PV01_bucket = [PV(rate + b) - PV(rate - b)] / (2 × b)
Gamma_bucket_per_bp² = [PV(rate + b) - 2 PV + PV(rate - b)] / b²
```

The response contains one bucket for every base- and quote-curve node, including tenor, time, canonical market quote ID and zero rate. It also returns:

- base-curve and quote-curve parallel PV01;
- sum of bucket PV01s as a reconciliation control;
- FX delta for a normalized 1% spot move and FX gamma per 1% squared;
- cross-currency-basis PV01;
- base- and quote-leg spread PV01.

Sensitivity bumps can be controlled in the request:

```json
{
  "sensitivities": {
    "rate_bump_bps": 1.0,
    "fx_bump_relative": 0.01,
    "basis_bump_bps": 1.0,
    "spread_bump_bps": 1.0
  }
}
```

The XVA section additionally returns counterparty CVA01, own DVA01, funding FVA01 and collateral COLVA01. These XVA spread sensitivities re-use the simulated EPE/ENE profile and therefore hold exposure fixed; they are not full market-risk re-simulations.

## Validation backlog

Before production use, the following remain mandatory:

1. audited holiday calendars and settlement conventions;
2. separate projection and discount curves, including basis instruments;
3. historical or implied volatility surfaces and calibrated correlations;
4. CSA terms, margin mechanics and MPOR;
5. netting-set aggregation and portfolio simulation;
6. wrong-way risk, collateral currency optionality and initial margin;
7. benchmark packs against QuantLib, ORE or an approved vendor;
8. independent model validation and numerical tolerances.
