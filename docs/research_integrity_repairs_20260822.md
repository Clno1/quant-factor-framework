# Research Integrity Repairs — 2026-08-22

This document records the economic/statistical contracts introduced by the
research-integrity repair branch. They are invariants, not presentation choices.

## 1. Price semantics

Published bars contain two different price families and they must never be
interchanged:

- `open/high/low/close`: executable, split-adjusted market prices.
- `adj_close`: dividend-adjusted total-return close.
- `execution_open` / `execution_close`: explicit aliases used for fills,
  tradability, dollar volume, ADV/capacity, replay valuation and forced exits.
- `total_return_close`: explicit alias of `adj_close` for factor/return research.
- `total_return_open = execution_open * (adj_close / execution_close)`: synthetic
  dividend-adjusted open used only for performance attribution.
- `total_return_returns = total_return_close.pct_change()`.

A next-open portfolio therefore enters/exits using executable prices while its
holding PnL is measured with total-return opens. This preserves dividends without
pretending a dividend-adjusted price was executable.

## 2. Historical neutralization

`LATEST_KNOWN_BACKFILL_NOT_PIT` sector metadata is not a valid historical
regressor. If industry neutralization is requested but only latest-known/static
classification exists, industry neutralization is skipped and the audit records:

- `requested_industry=true`
- `enabled_industry=false`
- the observed temporal policy
- an explicit non-PIT skip reason

Research continues without contaminated industry residualization. Once a true
date x ticker PIT classification matrix is published with
`classification_policy=PIT_EFFECTIVE_DATED`, the neutralizer can use it.

Market-cap neutralization is fail-closed. If requested, it requires a PIT date x
ticker matrix with an accepted PIT temporal policy; latest-known/static mcap
raises `NeutralizationDataError`.

## 3. Benchmark contract

Named research universes use the registry benchmark (`SP500 -> SPY`,
`NASDAQ100 -> QQQ`, etc.). The loader first looks inside the primary immutable
dataset version; if absent, it resolves the ticker from the immutable
`US_EQUITY_COVERAGE` publication.

Formal price-semantics-aware backtests require an explicit benchmark return
series. They no longer silently replace SPY/QQQ with an equal-weight universe.
The benchmark publication identity (ticker, universe, version/run IDs, target
session and checksums) is attached to the backtest data contract/result config.

Benchmark returns use the same `[t open, t+1 open)` total-return interval as the
strategy holding return.

## 4. Relative performance math

`ExcessAnnReturn` is the annualized arithmetic active return:

`mean(strategy_daily - benchmark_daily) * trading_days_per_year`

`InformationRatio` is:

`annualized_active_return / annualized_tracking_error`

Daily active returns are not geometrically compounded as if they were a standalone
wealth process. A separate `RelativeWealthAnnReturn` reports geometric strategy
wealth relative to benchmark wealth.

## 5. Overlapping IC inference

IC inference uses Newey-West/HAC covariance. Default lag is
`forward_periods - 1`; a 5-session overlapping forward horizon therefore uses lag
4. Formal confidence t-statistics, p-values, confidence intervals and downstream
FDR q-values are based on the HAC inference path.

## 6. IC censoring / delisting

An IC date with factor observations for securities whose forward outcomes are
selectively missing is no longer recomputed after `dropna()` on survivors.
Instead the date is invalidated by default (or raises under `censor_policy=fail`)
and diagnostics retain censored dates, counts and ticker samples.

`compute_ic(..., resolved_forward_returns=...)` accepts an audited forward-outcome
matrix when acquisition, bankruptcy, delisting or other terminal settlements are
resolved explicitly. A reviewed -100% outcome remains a valid -100% outcome;
forward compounding no longer turns it into a log-space infinity.

## 7. Event-level breakout / cup-handle backtest

`src/breakouts/historical_backtest.py` converts scanner states into frozen
historical events:

1. scanner at decision date `T` receives only data through `T`;
2. only configured state transitions (default `BREAKOUT`) create events;
3. a cooldown prevents repeated nearby signals from being counted as independent;
4. execution occurs at `T+1` executable open;
5. 1/5/20-session PnL uses total-return opens;
6. MAE/MFE use intraday high/low scaled by the same total-return adjustment;
7. round-trip cost drag is explicit;
8. unavailable entry/exit horizons are marked censored, never dropped silently;
9. outputs include overall, year and optional market-regime summaries.

Example:

```bash
python scripts/run_breakout_event_backtest.py \
  --tickers NVDA,TSLA,PLTR \
  --start 2020-01-01 \
  --end 2026-08-21 \
  --horizons 1,5,20 \
  --round-trip-cost-bps 20
```

The CLI writes `events.csv`, `summary.json`, and group summaries under
`outputs/breakouts/historical_event_backtest/` by default.

## Validation

The branch contains dedicated regression tests for price semantics, tradability,
relative metrics, HAC inference, censoring, PIT neutralization gates and event
timing. CI also runs the existing trading-integrity suite plus related data,
preprocessing, strategy, offline research, replay and web-backtest tests.
