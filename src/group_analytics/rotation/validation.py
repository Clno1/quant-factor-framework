"""Frozen-rule retrospective ETF study; never a production strategy selector."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import clean_table, metric_frame

FEATURES = ("abs1", "rs20", "rs60", "rs20_60", "source_score")


def block_interval(values, *, block=20, repeats=500):
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < max(40, block * 2):
        return None
    rng = np.random.default_rng(20260909)
    starts = rng.integers(0, len(values), size=(repeats, int(np.ceil(len(values) / block))))
    indices = (starts[..., None] + np.arange(block)) % len(values)
    means = values[indices.reshape(repeats, -1)[:, :len(values)]].mean(axis=1)
    return [float(x) for x in np.quantile(means, [.025, .975])]


def make_panel(frames, sessions, themes):
    """T+1 adjusted open -> T+h adjusted close, with every bar required."""
    prices = clean_table(pd.DataFrame({s: f.adj_close for s, f in frames.items()}), sessions)
    opens = clean_table(pd.DataFrame({s: f.open for s, f in frames.items()}), sessions)
    prices = prices.where(prices > 0)
    opens = opens.where(opens > 0)
    volumes = clean_table(pd.DataFrame({s: f.volume for s, f in frames.items()}), sessions)
    closes = {}
    for symbol, frame in frames.items():
        if "close" in getattr(frame, "columns", []):
            closes[symbol] = frame["close"]
    if closes:
        execution = clean_table(pd.DataFrame(closes), sessions).where(lambda table: table > 0)
    else:
        execution = prices * np.nan
    records = []
    for theme in themes:
        if not theme.proxy or theme.members:
            raise ValueError("Validation requires native ETFs, not current-member backcasts")
        p = metric_frame(theme, prices, volumes, strict=True, amount_verified=True,
                         execution_close=execution)
        compat = metric_frame(theme, prices, volumes, strict=False, execution_close=execution)
        p["source_score"] = compat.score
        p["rs20_60"] = (p.rs20 + p.rs60) / 2
        for horizon in (5,20):
            entry = opens[theme.proxy].shift(-1)
            benchmark_entry = opens[theme.benchmark].shift(-1)
            forward = prices[theme.proxy].shift(-horizon) / entry - 1
            benchmark_forward = prices[theme.benchmark].shift(-horizon) / benchmark_entry - 1
            continuous = prices[[theme.proxy,theme.benchmark]].notna().all(axis=1)
            continuous = continuous.rolling(horizon).sum().shift(-horizon).eq(horizon)
            valid = p.history_valid & continuous & entry.gt(0) & benchmark_entry.gt(0)
            for i in np.flatnonzero(valid.to_numpy()):
                records.append({"date":sessions[i], "exit_date":sessions[i+horizon],
                    "theme":theme.id,"cohort":theme.cohort,"horizon":horizon,
                    "state":p.state.iloc[i],"action":p.action.iloc[i],
                    "forward_return":forward.iloc[i],"benchmark_return":benchmark_forward.iloc[i],
                    "excess_return":forward.iloc[i]-benchmark_forward.iloc[i],
                    **{k:p[k].iloc[i] for k in FEATURES}})
    return pd.DataFrame(records), prices, opens


def period_name(day):
    return "development_2020_2022" if day.year <= 2022 else "validation_2023_2024" if day.year <= 2024 else "retrospective_test_2025_plus"


def summarize(panel, prices, opens, sessions, themes):
    if panel.empty:
        raise ValueError("No evaluable historical rows")
    panel = panel[panel.date >= pd.Timestamp("2020-01-01")].copy()
    # Purge any label spanning a split boundary; never train/select on test.
    panel = panel[[period_name(a)==period_name(b) for a,b in zip(panel.date,panel.exit_date)]]
    panel["period"] = panel.date.map(period_name)
    states, ic, portfolios = [], [], []
    for (cohort,period,horizon), group in panel.groupby(["cohort","period","horizon"]):
        for feature in FEATURES:
            daily = []
            for _, day in group.groupby("date"):
                pair = day[[feature,"excess_return"]].dropna()
                if len(pair)>=5 and pair[feature].nunique()>1 and pair.excess_return.nunique()>1:
                    daily.append(pair[feature].rank().corr(pair.excess_return.rank()))
            ic.append({"cohort":cohort,"period":period,"horizon":int(horizon),"feature":feature,
                       "dates":len(daily),"rank_ic":float(np.mean(daily)) if daily else None,
                       "ci95":block_interval(daily,block=horizon)})
        for state, subgroup in group.groupby("state"):
            # Average within day before bootstrapping, not independent stocks.
            daily = subgroup.groupby("date").excess_return.mean()
            states.append({"cohort":cohort,"period":period,"horizon":int(horizon),"state":state,
                           "dates":len(daily),"rows":len(subgroup),"mean_excess":daily.mean(),
                           "median_excess":daily.median(),"ci95":block_interval(daily,block=horizon),
                           "date_outperformance_rate":float((daily>0).mean()) if len(daily)>=40 else None})
    # Non-overlapping 20-session portfolios. Rebuy after the next open; no
    # interpolation, no overlapping-label NAV, benchmark uses identical clock.
    lookup = {t.id:t for t in themes}
    dates = sessions[sessions>=pd.Timestamp("2020-01-01")]
    anchors = dates[::20]
    by_method = {}
    for cohort in sorted(panel.cohort.unique()):
        subset = panel[(panel.cohort==cohort) & (panel.horizon==20)]
        expected = sum(t.cohort==cohort for t in themes)
        for method in (*FEATURES,"price_state"):
            for cost_bps in (0,10,25,50):
                nav, benchmark_nav, daily_nav, rounds, previous = 1.,1.,[1.],[],{}
                for date in anchors:
                    day = subset[subset.date==date].dropna(subset=list(FEATURES))
                    if len(day)!=expected:
                        continue  # Same complete ETF sample for every method.
                    pool = day[day.action.isin(["priority","price_watch","watch","focus","recover"])] if method=="price_state" else day
                    picks = pool.sort_values(["rs20" if method=="price_state" else method,"theme"],ascending=[False,True]).head(2)
                    weights = {x:.5 for x in picks.theme}  # Missing slots stay cash.
                    turnover = sum(abs(weights.get(x,0)-previous.get(x,0)) for x in set(weights)|set(previous))
                    previous = weights
                    i = sessions.get_loc(date)
                    holding_dates = sessions[i+1:i+21]
                    invested = sum(weights.values())
                    half_cost = cost_bps/20000
                    path = pd.Series(1-invested,index=holding_dates,dtype=float)
                    for theme_id,w in weights.items():
                        symbol=lookup[theme_id].proxy
                        path += w*(1-half_cost)*prices.loc[holding_dates,symbol]/opens.at[holding_dates[0],symbol]
                    path.iloc[-1] -= half_cost * (path.iloc[-1]-(1-invested))
                    daily_nav.extend((nav*path).tolist())
                    net=float(path.iloc[-1]-1)
                    benchmark_symbol=lookup[day.iloc[0].theme].benchmark
                    bnet=float((1-half_cost)**2*prices.at[holding_dates[-1],benchmark_symbol]/opens.at[holding_dates[0],benchmark_symbol]-1)
                    nav *= 1+net; benchmark_nav *= 1+bnet
                    rounds.append({"date":date.date().isoformat(),"period":period_name(date),
                                   "net_return":net,"benchmark_return":bnet,"excess":net-bnet,
                                   "turnover_l1":turnover,"names":list(weights)})
                series=np.asarray(daily_nav)
                item={"cohort":cohort,"method":method,"roundtrip_cost_bps":cost_bps,
                      "rounds":len(rounds),"nav":nav,"benchmark_nav":benchmark_nav,
                      "max_drawdown":float(np.min(series/np.maximum.accumulate(series)-1)),
                      "average_target_turnover_l1":float(np.mean([r['turnover_l1'] for r in rounds])) if rounds else None,
                      "periods":rounds}
                portfolios.append(item); by_method[(cohort,method,cost_bps)]=item
    paired=[]
    for cohort in sorted(panel.cohort.unique()):
        base=by_method[(cohort,"rs20",25)]
        base_returns={r["date"]:r for r in base["periods"]}
        for method in ("source_score","price_state"):
            differences=[r['net_return']-base_returns[r['date']]['net_return']
                         for r in by_method[(cohort,method,25)]["periods"]
                         if r['period']=='retrospective_test_2025_plus' and r['date'] in base_returns]
            paired.append({"cohort":cohort,"method":method,"baseline":"rs20","cost_bps":25,
                           "rounds":len(differences),"mean_20d_increment":float(np.mean(differences)) if differences else None,
                           "ci95":block_interval(differences,block=3),
                           "gate":"INSUFFICIENT_INDEPENDENT_PERIODS" if len(differences)<40 else "REQUIRES_PROSPECTIVE_CONFIRMATION"})
    return {"schema_version":"rotation.validation.v1","status":"RETROSPECTIVE_RESEARCH_ONLY",
            "promotion":"NOT_APPROVED", "labels":len(panel),"dates":int(panel.date.nunique()),
            "state_outcomes":states,"rank_ic":ic,"portfolios":portfolios,"paired_test":paired,
            "limits":["ETF selection and rules frozen today; history is not a truly untouched or prospective test",
                      "Current ETF holdings and custom baskets excluded from historical signals",
                      "Amount uses close×volume when close exists; missing close does not fall back to adj_close",
                      "Research downloader in scripts/research_group_rotation.py is still dividend-adjusted complete OHLCV; that path is not production canonical close and must not share the source_v1_compat name until field-for-field alignment",
                      "Roundtrip cost charged on all invested slots, even retained names; turnover is target-weight L1",
                      "Daily marked drawdown within trades; gaps with incomplete cohort skip equally; no annualization across skipped periods",
                      "Primary horizon 20 sessions; 5 secondary; split-spanning labels purged; circular date-block bootstrap 500 draws"]}
