"""Build a frozen daily candidate snapshot for one intraday session."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from src.breakouts.daily_data import (
    BreakoutDailyDataset,
    load_breakout_daily_dataset,
)
from src.breakouts.live.detector import ALGORITHM_VERSION, PARAMETER_VERSION
from src.breakouts.live.cup_handle import (
    CUP_HANDLE_ALGORITHM_VERSION,
    CUP_HANDLE_PARAMETER_VERSION,
    DailyCupSetup,
    detect_daily_cup,
)
from src.breakouts.live.models import DailyCandidate
from src.breakouts.live.settings import IntradayMonitorSettings
from src.breakouts.scanner import (
    BreakoutFilters,
    evaluate_daily_setup,
    scan_breakouts,
)


DailyDatasetLoader = Callable[..., BreakoutDailyDataset]


def _prepare_universe(
    settings: IntradayMonitorSettings,
    source: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, set[str]]:
    universe = source.copy()
    if "ticker" not in universe.columns:
        raise RuntimeError(f"{settings.universe} is missing ticker metadata")
    universe["ticker"] = universe["ticker"].astype(str).str.strip().str.upper()
    if not settings.include_etfs:
        if "asset_type" not in universe.columns:
            raise RuntimeError("US_ACTIVE is missing asset_type for stock-only monitoring")
        universe = universe.loc[
            universe["asset_type"].fillna("").astype(str).str.upper().eq("STOCK")
        ].copy()

    forced = set(settings.always_tickers) & set(universe["ticker"])
    liquidity = pd.to_numeric(
        universe.get(
            "current_dollar_volume",
            pd.Series(index=universe.index, dtype="float64"),
        ),
        errors="coerce",
    )
    eligible = universe.loc[
        (liquidity >= settings.broad_min_current_dollar_volume)
        | universe["ticker"].isin(forced)
    ].drop_duplicates(subset=["ticker"])
    return universe, eligible, forced


def _candidate_from_row(
    row: dict[str, Any],
    *,
    forced: bool,
    asof: str,
    frame: pd.DataFrame,
    cup_setup: DailyCupSetup | None = None,
) -> DailyCandidate | None:
    ticker = str(row.get("ticker") or "").strip().upper()
    if frame.empty:
        return None
    data = frame.copy()
    data.index = pd.to_datetime(data.index, errors="coerce")
    data = data.loc[
        (~data.index.isna()) & (data.index <= pd.Timestamp(asof))
    ].sort_index()
    required = {"high", "low", "close", "volume"}
    if len(data) < 21 or not required.issubset(data.columns):
        return None
    high = pd.to_numeric(data["high"], errors="coerce")
    low = pd.to_numeric(data["low"], errors="coerce")
    close = pd.to_numeric(data["close"], errors="coerce")
    valid = high.notna() & low.notna() & close.notna() & (low > 0)
    data, high, low, close = data.loc[valid], high.loc[valid], low.loc[valid], close.loc[valid]
    if len(data) < 21:
        return None
    adr = (high / low - 1.0) * 100.0
    cup = cup_setup or DailyCupSetup(False, "NOT_EVALUATED")
    return DailyCandidate(
        ticker=ticker,
        name=str(row.get("name") or ""),
        sector=str(row.get("sector") or ""),
        setup_score=int(row.get("score") or 0),
        daily_pivot=float(high.tail(20).max()),
        previous_high=float(high.iloc[-1]),
        adr20=float(row.get("adr_20d") or adr.tail(20).mean()),
        avg_dollar_volume20=float(row.get("avg_dollar_volume_20d") or 0.0),
        source_data_date=pd.Timestamp(data.index[-1]).strftime("%Y-%m-%d"),
        setup_qualified=bool(row.get("setup_qualified")),
        daily_status=str(row.get("status") or "FORMING").upper(),
        return_reference_close=float(close.iloc[-20]),
        adr_sum_19=float(adr.tail(19).sum()),
        forced_watch=forced,
        cup_qualified=cup.qualified,
        cup_rejection_reason=cup.rejection_reason,
        cup_left_rim_date=cup.left_rim_date,
        cup_right_rim_date=cup.right_rim_date,
        cup_bottom_date=cup.bottom_date,
        cup_left_rim=cup.left_rim,
        cup_right_rim=cup.right_rim,
        cup_bottom=cup.bottom,
        cup_depth_pct=cup.depth_pct,
        cup_width_sessions=cup.width_sessions,
        cup_rim_tolerance_pct=cup.rim_tolerance_pct,
        cup_volume_contraction_ratio=cup.volume_contraction_ratio,
        cup_score=cup.score,
    )


def build_daily_candidate_snapshot(
    settings: IntradayMonitorSettings,
    *,
    session_date: str,
    source_session: str,
    dataset_loader: DailyDatasetLoader = load_breakout_daily_dataset,
) -> dict[str, Any]:
    """Freeze the existing daily screen without importing alerts or Web code."""
    settings.validate()
    prepared: dict[str, Any] = {}

    def select_tickers(source: pd.DataFrame) -> list[str]:
        universe, eligible, forced = _prepare_universe(settings, source)
        prepared.update({
            "universe": universe,
            "eligible": eligible,
            "forced": forced,
        })
        return eligible["ticker"].tolist()

    dataset = dataset_loader(
        requested_universe=settings.universe,
        ticker_selector=select_tickers,
        end=source_session,
        min_latest_coverage=settings.min_exact_daily_coverage,
    )
    if prepared:
        universe = prepared["universe"]
        eligible = prepared["eligible"]
        forced = prepared["forced"]
    else:
        universe, eligible, forced = _prepare_universe(settings, dataset.universe)
    exact_tickers: list[str] = []
    for ticker in eligible["ticker"]:
        frame = dataset.frame(str(ticker))
        if frame.empty:
            continue
        latest = pd.Timestamp(frame.index.max()).normalize().strftime("%Y-%m-%d")
        if latest == source_session:
            exact_tickers.append(str(ticker))
    exact_coverage = len(exact_tickers) / len(eligible) if len(eligible) else 0.0
    if exact_coverage < settings.min_exact_daily_coverage:
        raise RuntimeError(
            "daily candidate coverage is stale: "
            f"{len(exact_tickers)}/{len(eligible)} exact at {source_session} "
            f"(< {settings.min_exact_daily_coverage:.0%})"
        )
    metadata = eligible.set_index("ticker", drop=False)
    names = metadata.get("name", pd.Series(dtype="object")).fillna("").to_dict()
    sectors = metadata.get("sector", pd.Series(dtype="object")).fillna("").to_dict()
    filters = BreakoutFilters(
        min_return_20d=settings.broad_min_return_20d,
        min_adr_20d=settings.broad_min_adr_20d,
        min_dollar_volume=0,
        min_avg_dollar_volume=settings.min_avg_dollar_volume,
        max_results=settings.max_symbols,
    )
    scan = scan_breakouts(
        exact_tickers,
        filters=filters,
        asof=source_session,
        names=names,
        sectors=sectors,
        data_universe=dataset.data_universe,
        dataset_version_id=dataset.dataset_version_id,
        frames=dataset.frames,
    )
    asof = source_session
    rows_by_ticker = {
        str(row["ticker"]).upper(): dict(row)
        for row in scan.get("rows") or []
    }

    cup_by_ticker: dict[str, DailyCupSetup] = {}
    cup_rejections: dict[str, int] = {}
    for ticker in exact_tickers:
        setup = detect_daily_cup(
            dataset.frame(ticker),
            settings=settings,
            asof=asof,
        )
        cup_by_ticker[ticker] = setup
        if not setup.qualified:
            cup_rejections[setup.rejection_reason] = (
                cup_rejections.get(setup.rejection_reason, 0) + 1
            )

    # Existing hourly behavior lets configured exceptions bypass only the broad
    # stage. They must still pass the strict screen before a live signal.
    permissive = BreakoutFilters(
        min_return_20d=-1_000_000,
        min_adr_20d=0,
        min_dollar_volume=0,
        min_avg_dollar_volume=0,
        max_results=settings.max_symbols,
    )
    for ticker in sorted(forced - set(rows_by_ticker)):
        row = evaluate_daily_setup(
            dataset.frame(ticker),
            ticker=ticker,
            filters=permissive,
            asof=asof,
            name=str(names.get(ticker) or ""),
            sector=str(sectors.get(ticker) or ""),
        )
        if row is not None:
            rows_by_ticker[ticker] = row

    cup_only = sorted(
        (
            ticker
            for ticker, setup in cup_by_ticker.items()
            if setup.qualified and ticker not in rows_by_ticker
        ),
        key=lambda ticker: (
            -cup_by_ticker[ticker].score,
            ticker,
        ),
    )
    for ticker in cup_only:
        row = evaluate_daily_setup(
            dataset.frame(ticker),
            ticker=ticker,
            filters=permissive,
            asof=asof,
            name=str(names.get(ticker) or ""),
            sector=str(sectors.get(ticker) or ""),
        )
        if row is None:
            row = {
                "ticker": ticker,
                "name": str(names.get(ticker) or ""),
                "sector": str(sectors.get(ticker) or ""),
                "score": int(round(cup_by_ticker[ticker].score)),
                "adr_20d": 0.0,
                "avg_dollar_volume_20d": 0.0,
                "setup_qualified": False,
                "status": "READY",
            }
        rows_by_ticker[ticker] = row

    ordered_rows = list(scan.get("rows") or [])
    ordered_tickers = [str(row["ticker"]).upper() for row in ordered_rows]
    qualified_cups = sorted(
        (
            ticker
            for ticker, setup in cup_by_ticker.items()
            if setup.qualified and ticker in rows_by_ticker
        ),
        key=lambda ticker: (-cup_by_ticker[ticker].score, ticker),
    )
    ordered_tickers = [
        *sorted(ticker for ticker in forced if ticker in rows_by_ticker),
        *[ticker for ticker in qualified_cups if ticker not in forced],
        *[ticker for ticker in ordered_tickers if ticker not in forced],
    ]
    ordered_tickers = list(dict.fromkeys(ordered_tickers))[: settings.max_symbols]
    candidates = [
        candidate
        for ticker in ordered_tickers
        if (
            candidate := _candidate_from_row(
                rows_by_ticker[ticker],
                forced=ticker in forced,
                asof=asof,
                frame=dataset.frame(ticker),
                cup_setup=cup_by_ticker.get(ticker),
            )
        ) is not None
    ]
    return {
        "session_date": session_date,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "algorithm_version": ALGORITHM_VERSION,
        "parameter_version": PARAMETER_VERSION,
        "source_data_date": asof,
        "universe": settings.universe,
        "data_universe": dataset.data_universe,
        "dataset_version_id": dataset.dataset_version_id,
        "data_contract": dataset.contract.to_dict(),
        "include_etfs": settings.include_etfs,
        "source_universe_count": len(universe),
        "eligible_count": len(eligible),
        "exact_daily_count": len(exact_tickers),
        "exact_daily_coverage": exact_coverage,
        "candidate_count": len(candidates),
        "cup_handle_daily": {
            "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION,
            "parameter_version": CUP_HANDLE_PARAMETER_VERSION,
            "evaluated_count": len(cup_by_ticker),
            "qualified_count": sum(
                setup.qualified for setup in cup_by_ticker.values()
            ),
            "selected_count": sum(candidate.cup_qualified for candidate in candidates),
            "rejection_counts": dict(sorted(cup_rejections.items())),
        },
        "rows": [candidate.to_dict() for candidate in candidates],
    }


def candidates_from_snapshot(snapshot: dict[str, Any]) -> list[DailyCandidate]:
    return [
        DailyCandidate.from_mapping(row)
        for row in snapshot.get("rows") or []
        if isinstance(row, dict)
    ]
