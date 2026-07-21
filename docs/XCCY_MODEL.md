# G10 XCCY model specification — MVP 0.2

## Scope

The engine prices constant-notional fixed/floating or floating/floating cross-currency swaps using a historical canonical market snapshot. It supports G10 currencies and reports every market-data source used.

## FX convention

`FX.BASEQUOTE.SPOT` means units of `QUOTE` for one unit of `BASE`.

With continuously compounded OIS zero curves and model basis `b`, the forward used by the engine is:

```text
F_base,quote(t,T) = S(t) × DF_base(t,T) / DF_quote(t,T) × exp(b × (T-t))
```

`BASIS.BASEQUOTE.XCCY` is therefore defined as an additive forward-drift spread. Reversing the currency pair reverses its sign. Cross-pair basis is triangulated through USD when both USD legs are available.

## Curves

Canonical OIS pillars use:

```text
RATE.<CCY>.OIS.<TENOR>
```

Zero rates are continuously compounded and linearly interpolated in maturity. Discount factors are:

```text
DF(0,T) = exp(-z(T) × T)
```

The current builder is intentionally transparent. It is not yet a full par-instrument bootstrap.

## Cash flows

- fixed coupons use the request fixed rate;
- floating coupons use the single OIS curve forward plus the request spread;
- initial and final notional exchanges are independently selectable;
- if initial exchange has already occurred, it is excluded;
- notionals do not reset.

## Discounting representations

### Native OIS

Each leg is discounted with its own OIS curve and converted to the reporting currency at valuation-date spot.

### Base collateral

Quote cash flows are converted to base currency using forward FX and discounted with the base OIS curve.

### Quote collateral

Base cash flows are converted to quote currency using forward FX and discounted with the quote OIS curve.

At zero basis, all three representations are regression-tested to agree within floating-point tolerance.

## Risk diagnostics

The engine uses central differences:

```text
DV01_base = [PV(r_base + 1bp) - PV(r_base - 1bp)] / 2
```

The mixed FX/rate gamma P&L is:

```text
1/4 × [PV(S+hS,r+hr) - PV(S+hS,r-hr)
     - PV(S-hS,r+hr) + PV(S-hS,r-hr)]
```

where `hS = 1% × spot` and `hr = 1 bp`. The same diagnostic is produced for the base curve, quote curve and XCCY basis.

## Exposure simulation

The process contains four correlated factors:

- lognormal FX;
- Gaussian mean-reverting base rate shift;
- Gaussian mean-reverting quote rate shift;
- Gaussian mean-reverting basis shift.

The correlation matrix is validated through Cholesky decomposition. The random seed is part of the request and response contract.

The output profile contains deterministic forward MTM, expected MTM, EPE, ENE, PFE, collateralized EPE/ENE, expected collateral and an initial-margin proxy.

## XVA

The XVA formulas are exposure integrations intended for engineering validation:

```text
CVA = LGD_cp × Σ DF(t_i) × EPE(t_i) × marginal PD_cp(t_i)
DVA = LGD_own × Σ DF(t_i) × |ENE(t_i)| × marginal PD_own(t_i)
FVA = funding spread × ∫ collateralized EPE(t) dt
COLVA = collateral spread × ∫ expected collateral(t) dt
MVA = IM funding spread × ∫ IM proxy(t) dt
KVA = capital cost × capital multiplier × ∫ EPE(t) dt
Total XVA = -CVA + DVA - FVA - COLVA - MVA - KVA
```

## Known limitations

- weekend-only calendars;
- no historical fixings;
- no resettable notionals;
- no curve instrument bootstrap;
- parallel stochastic rate shifts only;
- no stochastic volatility or smile;
- no wrong-way risk;
- simplified collateral and IM;
- no netting across multiple trades yet.
