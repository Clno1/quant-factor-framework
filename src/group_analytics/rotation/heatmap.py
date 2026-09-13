"""Same-day market-cap treemap from US_EQUITY_COVERAGE.

Explanation only. Never writes production, never changes assign_priority.
Industry and cap are LATEST_KNOWN_BACKFILL_NOT_PIT — today only, not history.
"""
from __future__ import annotations

import math
from datetime import date
from urllib.parse import quote

import pandas as pd

from src.data.security_master import CLASSIFICATION_POLICY, MARKET_CAP_POLICY, UNKNOWN_CLASSIFICATION
from src.data.universe_ids import US_EQUITY_COVERAGE
from src.group_analytics.calendar import CalendarUnavailableError, _calendar, latest_completed_session

HEATMAP_VERSION = "coverage-mcap-treemap-v1"
HEATMAP_WINDOWS = (1, 5, 20)
HEATMAP_NOTE = "行业分类与市值为最新已知回填，非PIT；仅当日观察，不用于历史回看"
PRICE_BASIS = "adj_close"
LOOKBACK_CALENDAR_DAYS = 45


def _label(value, fallback=UNKNOWN_CLASSIFICATION):
    if value is None or (not isinstance(value, (list, dict, tuple)) and pd.isna(value)):
        return fallback
    text = str(value).strip()
    return text or fallback


def _finite_return(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def unavailable(reason, **extra):
    payload = {
        "version": HEATMAP_VERSION,
        "status": "unavailable",
        "reason": reason,
        "classification_policy": CLASSIFICATION_POLICY,
        "market_cap_policy": MARKET_CAP_POLICY,
        "pit_safe_for_history": False,
        "price_basis": PRICE_BASIS,
        "source_session": extra.pop("source_session", None),
        "windows": {str(window): None for window in HEATMAP_WINDOWS},
        "sectors": [],
        "counts": None,
        "notes": [HEATMAP_NOTE],
    }
    payload.update(extra)
    return payload


def _label(value, fallback=UNKNOWN_CLASSIFICATION):
    text = str(value or "").strip()
    return text or fallback


def _href(ticker):
    return "/breakouts/" + quote(str(ticker), safe="")


def _stock_return(end, start, ticker):
    if ticker not in end.index or ticker not in start.index:
        return None
    first = start[ticker]
    last = end[ticker]
    if pd.isna(first) or pd.isna(last) or float(first) <= 0:
        return None
    return _finite_return(float(last) / float(first) - 1.0)


def _weighted_return(caps, returns):
    mask = returns.notna()
    weights = caps[mask]
    values = returns[mask]
    if weights.empty or float(weights.sum()) <= 0:
        return None
    return float((weights * values).sum() / weights.sum())


def _node_returns(frame):
    payload = {}
    for window in HEATMAP_WINDOWS:
        key = f"ret{window}"
        cap_key = f"market_cap{window}"
        n_key = f"n{window}"
        ok = frame[key].notna()
        payload[key] = _weighted_return(frame.loc[ok, "market_cap"], frame.loc[ok, key])
        payload[cap_key] = float(frame.loc[ok, "market_cap"].sum()) if ok.any() else 0.0
        payload[n_key] = int(ok.sum())
    payload["market_cap"] = float(frame["market_cap"].sum()) if len(frame) else 0.0
    payload["n"] = int(len(frame))
    return payload


def build_coverage_heatmap(members, prices, *, source_session, windows=HEATMAP_WINDOWS, sessions=None):
    """Build a two-level cap-weighted tree. Does not mutate production rows."""
    source = pd.Timestamp(source_session).normalize()
    universe = members.copy()
    universe["ticker"] = universe["ticker"].astype(str).str.strip().str.upper()
    universe = universe.loc[universe["ticker"].ne("")].drop_duplicates("ticker", keep="last")
    if "is_current_member" in universe.columns:
        universe = universe.loc[universe["is_current_member"].astype(bool)]
    coverage_current = int(len(universe))
    if "coverage_role" in universe.columns:
        benchmark = universe["coverage_role"].astype(str).eq("BENCHMARK_ONLY")
    else:
        benchmark = pd.Series(False, index=universe.index)
    eligible = universe.loc[~benchmark].copy()
    if "sector" not in eligible.columns:
        eligible["sector"] = UNKNOWN_CLASSIFICATION
    else:
        eligible["sector"] = eligible["sector"].map(_label)
    if "sub_industry" not in eligible.columns:
        eligible["sub_industry"] = UNKNOWN_CLASSIFICATION
    else:
        eligible["sub_industry"] = eligible["sub_industry"].map(_label)
    if "name" not in eligible.columns:
        eligible["name"] = eligible["ticker"]
    eligible["name"] = eligible["name"].map(lambda value: _label(value, fallback=""))
    eligible.loc[eligible["name"].eq(""), "name"] = eligible["ticker"]
    eligible["market_cap"] = pd.to_numeric(eligible.get("market_cap"), errors="coerce")
    no_cap = ~eligible["market_cap"].gt(0)
    usable = eligible.loc[~no_cap].copy()

    bars = prices.copy()
    bars["ticker"] = bars["ticker"].astype(str).str.strip().str.upper()
    bars["date"] = pd.to_datetime(bars["date"], errors="coerce").dt.normalize()
    if "adj_close" not in bars.columns:
        return unavailable("PRICE_GAP", source_session=source.date().isoformat())
    bars["adj_close"] = pd.to_numeric(bars["adj_close"], errors="coerce")
    bars = bars.dropna(subset=["date", "ticker", "adj_close"])
    bars = bars.loc[bars["adj_close"] > 0]
    wide = bars.pivot_table(index="date", columns="ticker", values="adj_close", aggfunc="last")
    if sessions is None:
        session_index = wide.index.sort_values()
    else:
        session_index = pd.DatetimeIndex(pd.to_datetime(list(sessions))).normalize().unique().sort_values()
    session_index = session_index[session_index <= source]
    wide = wide.reindex(session_index)
    if source not in wide.index:
        return unavailable("PRICE_GAP", source_session=source.date().isoformat())
    loc = int(session_index.get_loc(source))
    end = wide.loc[source]
    for window in windows:
        start_loc = loc - int(window)
        if start_loc < 0:
            usable[f"ret{window}"] = pd.Series([None] * len(usable), index=usable.index)
            continue
        start = wide.iloc[start_loc]
        usable[f"ret{window}"] = [_stock_return(end, start, ticker) for ticker in usable["ticker"]]

    window_counts = {}
    for window in windows:
        key = f"ret{window}"
        have = usable[key].notna()
        window_counts[str(window)] = {
            "in_tree": int(have.sum()),
            "no_market_cap": int(no_cap.sum()),
            "no_price": int((~have).sum()),
        }

    sectors = []
    for sector_name, sector_rows in usable.groupby("sector", sort=True):
        industries = []
        for industry_name, industry_rows in sector_rows.groupby("sub_industry", sort=True):
            stocks = []
            for record in industry_rows.sort_values("market_cap", ascending=False).to_dict("records"):
                stock = {
                    "ticker": record["ticker"],
                    "name": record["name"],
                    "market_cap": float(record["market_cap"]),
                    "href": _href(record["ticker"]),
                }
                for window in windows:
                    stock[f"ret{window}"] = _finite_return(record.get(f"ret{window}"))
                stocks.append(stock)
            industries.append({
                "name": str(industry_name),
                "stocks": stocks,
                **_node_returns(industry_rows),
            })
        sectors.append({
            "name": str(sector_name),
            "industries": industries,
            **_node_returns(sector_rows),
        })

    counts = {
        "coverage_current": coverage_current,
        "benchmark_only": int(benchmark.sum()),
        "eligible": int(len(eligible)),
        "windows": window_counts,
    }
    return {
        "version": HEATMAP_VERSION,
        "status": "available",
        "reason": None,
        "classification_policy": CLASSIFICATION_POLICY,
        "market_cap_policy": MARKET_CAP_POLICY,
        "pit_safe_for_history": False,
        "price_basis": PRICE_BASIS,
        "source_session": source.date().isoformat(),
        "windows": {str(window): True for window in windows},
        "sectors": sectors,
        "counts": counts,
        "notes": [HEATMAP_NOTE],
    }


def _session_lookback(source, *, calendar=None):
    cal = _calendar(calendar)
    start = (pd.Timestamp(source) - pd.Timedelta(days=LOOKBACK_CALENDAR_DAYS)).date().isoformat()
    end = pd.Timestamp(source).date().isoformat()
    sessions = cal.sessions_in_range(start, end)
    if getattr(sessions, "tz", None) is not None:
        sessions = sessions.tz_localize(None)
    return pd.DatetimeIndex(sessions).normalize()


def load_coverage_heatmap(*, source_session=None, historical=False, reader=None, now=None, calendar=None):
    """Read published coverage. Never calls a market-data provider."""
    if historical:
        return unavailable("HISTORICAL_VIEW_FORBIDDEN", source_session=source_session)
    try:
        expected = latest_completed_session(now=now, calendar=calendar).date().isoformat()
    except CalendarUnavailableError:
        return unavailable("COVERAGE_NOT_PUBLISHED", source_session=source_session)
    if source_session and str(source_session) != expected:
        return unavailable("SESSION_MISMATCH", source_session=str(source_session), expected_session=expected)
    session = expected
    from src.data.foundation import DataFoundationError, MarketDataReader, NoPublishedDataError

    market = reader or MarketDataReader()
    try:
        version = market.require_latest(US_EQUITY_COVERAGE, require_price_semantics=True)
        target = date.fromisoformat(str(version.target_session)) if not isinstance(version.target_session, date) else version.target_session
        if target.isoformat() != session:
            return unavailable(
                "STALE_TARGET_SESSION",
                source_session=session,
                coverage_session=target.isoformat(),
            )
        members = market.load_universe(US_EQUITY_COVERAGE, current_only=True, version=version)
        if members is None or members.empty:
            return unavailable("EMPTY_UNIVERSE", source_session=session)
        sessions = _session_lookback(session, calendar=calendar)
        if session not in {str(item.date()) for item in sessions}:
            return unavailable("PRICE_GAP", source_session=session)
        prices = market.load_bars(
            US_EQUITY_COVERAGE,
            start=sessions[0].date().isoformat(),
            end=session,
            version=version,
        )
        if prices is None or prices.empty:
            return unavailable("PRICE_GAP", source_session=session)
        return build_coverage_heatmap(members, prices, source_session=session, sessions=sessions)
    except (NoPublishedDataError, DataFoundationError, FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        return unavailable("COVERAGE_NOT_PUBLISHED", source_session=session)
