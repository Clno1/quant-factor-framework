"""Two-stage live momentum scan used by the scheduled alert worker."""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from src.alerts.config import AlertSettings
from src.alerts.state import SIGNAL_RANK
from src.breakouts import (
    BreakoutFilters,
    build_intraday_snapshot,
    evaluate_daily_setup,
    load_intraday_1min,
    load_market_regime,
    scan_breakouts,
)
from src.breakouts.daily_data import (
    BreakoutDailyDataset,
    load_breakout_daily_dataset,
)
from src.data.fmp import get_batch_quotes, get_exchange_market_hours
from src.utils.logger import get_logger

log = get_logger(__name__)
_NEW_YORK = ZoneInfo("America/New_York")
DailyDatasetLoader = Callable[..., BreakoutDailyDataset]


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def market_hours_snapshot(settings: AlertSettings) -> dict[str, Any]:
    try:
        return get_exchange_market_hours(settings.exchange)
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot read %s market hours: %s", settings.exchange, exc)
        return {
            "exchange": settings.exchange,
            "timezone": "America/New_York",
            "isMarketOpen": False,
            "error": str(exc),
        }


def _forced_tickers(settings: AlertSettings) -> set[str]:
    tickers = set(settings.always_tickers)
    try:
        from src.watchlists import list_watchlists, load_watchlist

        for entry in list_watchlists():
            watchlist = load_watchlist(str(entry.get("id") or ""))
            if watchlist is not None:
                tickers.update(watchlist.tickers())
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot include Watchlist tickers in alerts: %s", exc)
    try:
        from src.papertrading import list_accounts
        from src.papertrading.store import load_table

        for account in list_accounts():
            account_id = str(account.get("id") or "")
            positions = load_table(account_id, "positions")
            if positions.empty or "ticker" not in positions.columns:
                continue
            if "quantity" in positions.columns:
                positions = positions[pd.to_numeric(positions["quantity"], errors="coerce") > 0]
            tickers.update(positions["ticker"].dropna().astype(str).str.upper())
    except Exception as exc:  # noqa: BLE001
        log.warning("Cannot include paper positions in alerts: %s", exc)
    return {ticker for ticker in tickers if ticker}


def _broad_pool(
    settings: AlertSettings,
    forced: set[str],
    daily: BreakoutDailyDataset,
) -> tuple[list[str], pd.DataFrame, dict[str, Any], set[str], set[str], int]:
    (
        universe,
        eligible,
        effective_forced,
        excluded_forced,
        source_universe_count,
    ) = _prepare_broad_universe(settings, forced, daily.universe)
    names = eligible.set_index("ticker").get("name", pd.Series(dtype="object")).fillna("").to_dict()
    sectors = eligible.set_index("ticker").get("sector", pd.Series(dtype="object")).fillna("").to_dict()
    scan = scan_breakouts(
        eligible["ticker"],
        filters=BreakoutFilters(
            min_return_20d=settings.broad_min_return_20d,
            min_adr_20d=settings.broad_min_adr_20d,
            min_dollar_volume=0,
            min_avg_dollar_volume=settings.min_avg_dollar_volume,
            max_results=settings.broad_max_symbols,
        ),
        names=names,
        sectors=sectors,
        data_universe=daily.data_universe,
        dataset_version_id=daily.dataset_version_id,
        frames=daily.frames,
    )
    tickers = [str(row["ticker"]) for row in scan["rows"]]
    tickers = list(dict.fromkeys([*tickers, *sorted(effective_forced)]))
    return (
        tickers,
        universe,
        scan,
        effective_forced,
        excluded_forced,
        source_universe_count,
    )


def _prepare_broad_universe(
    settings: AlertSettings,
    forced: set[str],
    source: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, set[str], set[str], int]:
    universe = source.copy()
    if "ticker" not in universe.columns:
        raise RuntimeError(f"{settings.universe} 缺少 ticker 行情元数据")
    universe["ticker"] = universe["ticker"].astype(str).str.upper()
    source_universe_count = len(universe)
    if "asset_type" not in universe.columns:
        raise RuntimeError(
            f"{settings.universe} 缺少 asset_type，无法可靠执行资产类型过滤"
        )
    asset_types = universe["asset_type"].fillna("").astype(str).str.upper()
    allowed_types = {"STOCK", "ETF"} if settings.include_etfs else {"STOCK"}
    universe = universe.loc[asset_types.isin(allowed_types)].copy()
    universe = universe.drop_duplicates(subset=["ticker"], keep="first")
    allowed_tickers = set(universe["ticker"])
    effective_forced = forced & allowed_tickers
    excluded_forced = forced - effective_forced
    current_liquidity = pd.to_numeric(
        universe.get("current_dollar_volume", pd.Series(index=universe.index, dtype="float64")),
        errors="coerce",
    )
    eligible = universe[
        (current_liquidity >= settings.broad_min_current_dollar_volume)
        | universe["ticker"].isin(effective_forced)
    ].copy()
    return (
        universe,
        eligible,
        effective_forced,
        excluded_forced,
        source_universe_count,
    )


def _quote_datetime(quote: pd.Series) -> datetime:
    timestamp = _finite(quote.get("timestamp"))
    if timestamp is None:
        return datetime.now(_NEW_YORK)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(_NEW_YORK)


def _provisional_daily_frame(
    frame: pd.DataFrame,
    quote: pd.Series,
    quote_date: pd.Timestamp,
) -> pd.DataFrame:
    required = ["open", "high", "low", "close", "volume"]
    if frame.empty or any(column not in frame.columns for column in required):
        return pd.DataFrame(columns=required)
    price = _finite(quote.get("price"))
    if price is None or price <= 0:
        return pd.DataFrame(columns=required)
    open_price = _finite(quote.get("open"), price) or price
    high = max(price, open_price, _finite(quote.get("dayHigh"), price) or price)
    low = min(price, open_price, _finite(quote.get("dayLow"), price) or price)
    if low <= 0:
        low = min(price, open_price)
    volume = max(0.0, _finite(quote.get("volume"), 0.0) or 0.0)

    data = frame[required].copy()
    data.index = pd.to_datetime(data.index).normalize()
    data = data.loc[data.index <= quote_date].copy()
    data.loc[quote_date, required] = [open_price, high, low, price, volume]
    return data.sort_index()


def _completed_avg_dollar_volume(
    frame: pd.DataFrame,
    *,
    quote_date: pd.Timestamp,
    market_open: bool,
) -> float:
    if frame.empty or not {"close", "volume"}.issubset(frame.columns):
        return 0.0
    completed = frame.copy()
    completed.index = pd.to_datetime(completed.index).normalize()
    if market_open:
        completed = completed.loc[completed.index < quote_date]
    else:
        completed = completed.loc[completed.index <= quote_date]
    values = (
        pd.to_numeric(completed["close"], errors="coerce")
        * pd.to_numeric(completed["volume"], errors="coerce")
    ).dropna()
    return float(values.tail(20).mean()) if not values.empty else 0.0


def _signal_type(metric: dict[str, Any]) -> str:
    status = str(metric.get("status") or "FORMING").upper()
    if status == "BREAKOUT":
        return "BREAKOUT"
    if status == "READY":
        return "READY"
    return "CANDIDATE"


def _intraday_enrich(
    rows: list[dict[str, Any]],
    settings: AlertSettings,
    session_date: str,
) -> None:
    for row in rows[: settings.intraday_max_symbols]:
        ticker = str(row["ticker"])
        frame, source = load_intraday_1min(
            ticker,
            refresh=True,
            end=session_date,
            lookback_days=settings.intraday_lookback_days,
        )
        snapshot = build_intraday_snapshot(
            frame,
            interval=settings.intraday_interval,
            session_date=session_date,
        )
        row["intraday_source"] = source
        row["intraday_session_date"] = snapshot.get("session_date")
        bars = snapshot.get("bars") or []
        latest = bars[-1] if bars else {}
        ma10, ma20, ma50 = (
            _finite(latest.get("ma10")),
            _finite(latest.get("ma20")),
            _finite(latest.get("ma50")),
        )
        ma_aligned = (
            ma10 is not None and ma20 is not None and ma50 is not None
            and ma10 > ma20 > ma50
        )
        opening_ranges = snapshot.get("opening_ranges") or {}
        opening_range = opening_ranges.get("60") or opening_ranges.get("30") or {}
        range_break = bool(opening_range.get("triggered") and opening_range.get("current_above"))
        row["intraday_ma_aligned"] = ma_aligned
        row["intraday_range_break"] = range_break
        row["intraday_last_timestamp"] = snapshot.get("last_timestamp")
        if range_break and ma_aligned:
            row["signal_type"] = "OPENING_RANGE_BREAK"
            row["intraday_trigger"] = (
                f"{settings.intraday_interval}分钟 MA10 > MA20 > MA50，且站上开盘区间高点"
            )


def run_live_alert_scan(
    settings: AlertSettings,
    *,
    market_hours: dict[str, Any] | None = None,
    include_intraday: bool | None = None,
    dataset_loader: DailyDatasetLoader = load_breakout_daily_dataset,
) -> dict[str, Any]:
    market_hours = dict(market_hours or market_hours_snapshot(settings))
    market_open = bool(market_hours.get("isMarketOpen"))
    forced = _forced_tickers(settings)

    def select_tickers(source: pd.DataFrame) -> list[str]:
        _, eligible, _, _, _ = _prepare_broad_universe(settings, forced, source)
        selected = eligible["ticker"].tolist()
        source_tickers = set(source["ticker"].astype(str).str.upper())
        if "QQQ" in source_tickers:
            selected.append("QQQ")
        return list(dict.fromkeys(selected))

    daily = dataset_loader(
        requested_universe=settings.universe,
        ticker_selector=select_tickers,
        min_latest_coverage=settings.min_exact_daily_coverage,
    )
    (
        broad_tickers,
        universe,
        broad_scan,
        forced,
        excluded_forced,
        source_universe_count,
    ) = _broad_pool(settings, forced, daily)
    quotes = get_batch_quotes(broad_tickers, chunk_size=settings.quote_chunk_size)
    if quotes.empty:
        raise RuntimeError("FMP batch-quote returned no rows for the broad momentum pool")

    quote_times = [_quote_datetime(row) for _, row in quotes.iterrows()]
    quote_time = max(quote_times)
    quote_date = pd.Timestamp(quote_time.date())
    session_date = quote_date.strftime("%Y-%m-%d")
    metadata = universe.set_index("ticker", drop=False)
    strict_filters = BreakoutFilters(
        min_return_20d=settings.strict_min_return_20d,
        min_adr_20d=settings.strict_min_adr_20d,
        min_dollar_volume=settings.strict_min_dollar_volume,
        min_avg_dollar_volume=0,
        max_results=1000,
    )

    rows: list[dict[str, Any]] = []
    unavailable: list[str] = []
    for ticker in broad_tickers:
        if ticker not in quotes.index:
            unavailable.append(ticker)
            continue
        quote = quotes.loc[ticker]
        frame = daily.frame(ticker)
        if frame.empty:
            unavailable.append(ticker)
            continue
        name = str(quote.get("name") or "")
        sector = ""
        asset_type = ""
        if ticker in metadata.index:
            meta = metadata.loc[ticker]
            name = name or str(meta.get("name") or "")
            sector = str(meta.get("sector") or "")
            asset_type = str(meta.get("asset_type") or "")
        provisional = _provisional_daily_frame(frame, quote, quote_date)
        metric = evaluate_daily_setup(
            provisional,
            ticker=ticker,
            filters=strict_filters,
            asof=quote_date,
            name=name,
            sector=sector,
        )
        if metric is None:
            unavailable.append(ticker)
            continue
        completed_avg = _completed_avg_dollar_volume(
            frame,
            quote_date=quote_date,
            market_open=market_open,
        )
        metric["avg_dollar_volume_20d"] = completed_avg
        metric["base_checks"]["avg_dollar_volume"] = (
            completed_avg >= settings.strict_min_avg_dollar_volume
        )
        metric["base_pass"] = all(metric["base_checks"].values())
        if not metric["base_pass"]:
            continue
        metric["asset_type"] = asset_type
        metric["quote_timestamp"] = _quote_datetime(quote).isoformat(timespec="seconds")
        metric["signal_type"] = _signal_type(metric)
        metric["forced_watch"] = ticker in forced
        rows.append(metric)

    rows.sort(key=lambda row: (
        -SIGNAL_RANK.get(str(row.get("signal_type") or "CANDIDATE"), 1),
        -int(row.get("score") or 0),
        -float(row.get("return_20d") or 0),
    ))
    use_intraday = settings.intraday_enabled if include_intraday is None else include_intraday
    if use_intraday and rows:
        _intraday_enrich(rows, settings, session_date)
        rows.sort(key=lambda row: (
            -SIGNAL_RANK.get(str(row.get("signal_type") or "CANDIDATE"), 1),
            -int(row.get("score") or 0),
            -float(row.get("return_20d") or 0),
        ))

    try:
        market_regime = load_market_regime(
            asof=session_date,
            symbol="QQQ",
            fetch_missing=False,
            data_universe=daily.data_universe,
            dataset_version_id=daily.dataset_version_id,
            frame=daily.frame("QQQ"),
        )
    except Exception as exc:  # noqa: BLE001
        market_regime = {"symbol": "QQQ", "passed": False, "error": str(exc)}

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_date": session_date,
        "quote_time": quote_time.isoformat(timespec="seconds"),
        "market_hours": market_hours,
        "market_regime": market_regime,
        "universe": settings.universe,
        "data_universe": daily.data_universe,
        "dataset_version_id": daily.dataset_version_id,
        "data_contract": daily.contract.to_dict(),
        "asset_scope": "stocks_and_etfs" if settings.include_etfs else "stocks",
        "include_etfs": settings.include_etfs,
        "source_universe_count": source_universe_count,
        "universe_count": len(universe),
        "eligible_count": int((
            pd.to_numeric(universe.get("current_dollar_volume"), errors="coerce")
            >= settings.broad_min_current_dollar_volume
        ).sum()),
        "broad_scan_asof": broad_scan.get("asof"),
        "broad_count": len(broad_tickers),
        "quote_count": len(quotes),
        "strict_count": len(rows),
        "forced_tickers": sorted(forced),
        "excluded_forced_tickers": sorted(excluded_forced),
        "unavailable_tickers": unavailable,
        "intraday_enabled": bool(use_intraday),
        "rows": rows,
    }


__all__ = ["market_hours_snapshot", "run_live_alert_scan"]
