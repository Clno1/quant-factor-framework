"""Application services for breakout universes and cached daily scans."""
from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import threading
from typing import Any

import pandas as pd

from src.breakouts.daily_data import load_breakout_daily_dataset
from src.breakouts.broad_daily_data import load_broad_breakout_universe
from src.breakouts.scan_cache import (
    load_scan_cache, save_scan_cache, request_scan_build, process_scan_build_requests,
)
from src.breakouts.scanner import (
    BreakoutFilters,
    load_market_regime,
    scan_breakouts,
)
from src.data.access import load_published_universe, resolve_published_version
from src.data.foundation import DataFoundationError
from src.data.universe_ids import (
    US_LIQUID_5M,
    resolve_market_data_universe,
    watchlist_snapshot_data_universe,
)


BREAKOUT_UNIVERSE_LABELS = {
    "US_ACTIVE": "美股活跃标的 · 股票 + ETF · NASDAQ / NYSE / AMEX",
    "SP500": "S&P 500",
    "MAG7": "科技龙头 · MAG7",
}

_SCAN_LOCK = threading.Lock()


class BreakoutApplicationError(RuntimeError):
    """Base error for caller-visible breakout application failures."""


class BreakoutWatchlistNotFoundError(BreakoutApplicationError):
    """Raised when a requested Watchlist no longer exists."""


class BreakoutScanNotReadyError(BreakoutApplicationError):
    """Raised when a caller forbids an expensive cache-miss rebuild."""


class UnknownBreakoutUniverseError(BreakoutApplicationError):
    """Raised when a preset universe is not available to the caller."""


def normalize_breakout_universe(raw: str | None) -> str:
    value = str(raw or "").strip()
    if not value:
        return "US_ACTIVE"
    if value.lower().startswith("watchlist:"):
        return f"watchlist:{value.split(':', 1)[1].strip()}"
    return value.upper()


def _normalized_enabled_universes(enabled_universes: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(
        str(value).strip().upper()
        for value in enabled_universes
        if str(value).strip()
    ))


def breakout_universe_options(
    enabled_universes: Iterable[str],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Return preset and Watchlist options without depending on the Web layer."""
    from src.watchlists import list_watchlists

    enabled = _normalized_enabled_universes(enabled_universes)
    values = ["US_ACTIVE", *[value for value in enabled if value != "US_ACTIVE"]]
    options = [
        {"value": value, "label": BREAKOUT_UNIVERSE_LABELS.get(value, value)}
        for value in dict.fromkeys(values)
    ]
    return options, list_watchlists()


def resolve_breakout_universe(
    raw: str | None,
    *,
    enabled_universes: Iterable[str],
    dataset_version_id: str | None = None,
) -> dict[str, Any]:
    """Resolve a preset universe or Watchlist into scanner inputs."""
    universe = normalize_breakout_universe(raw)
    if universe.lower().startswith("watchlist:"):
        from src.watchlists import load_watchlist

        watchlist_id = universe.split(":", 1)[1].strip()
        watchlist = load_watchlist(watchlist_id)
        if watchlist is None:
            raise BreakoutWatchlistNotFoundError(f"股票池不存在: {watchlist_id}")
        watchlist.validate()
        snapshot = watchlist.to_dict()
        items = snapshot.get("items") or []
        tickers = [str(item.get("ticker") or "").upper() for item in items]
        names = {
            str(item.get("ticker") or "").upper(): str(item.get("name") or "")
            for item in items
        }
        data_universe = watchlist_snapshot_data_universe(snapshot)
        try:
            published = load_published_universe(
                requested_universe=universe,
                data_universe=data_universe,
                dataset_version_id=dataset_version_id,
            )
        except DataFoundationError as exc:
            raise BreakoutApplicationError(
                "该股票池的统一行情仍在准备中，请等待数据任务发布后再扫描"
            ) from exc
        return {
            "universe": universe,
            "label": watchlist.name,
            "watchlist_snapshot_sha256": hashlib.sha256(
                json.dumps(snapshot, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
            ).hexdigest(),
            "tickers": [ticker for ticker in tickers if ticker],
            "names": names,
            "sectors": {},
            "current_dollar_volume": {},
            "data_universe": data_universe,
            "dataset_version_id": published.version.version_id,
        }

    enabled = _normalized_enabled_universes(enabled_universes)
    if universe not in enabled and universe != "US_ACTIVE":
        raise UnknownBreakoutUniverseError(f"未知股票池: {universe}")

    data_universe = resolve_market_data_universe(universe)
    try:
        published = (
            load_broad_breakout_universe(
                dataset_version_id=dataset_version_id,
            )
            if data_universe == US_LIQUID_5M
            else load_published_universe(
                requested_universe=universe,
                data_universe=data_universe,
                dataset_version_id=dataset_version_id,
            )
        )
        metadata = published.universe
    except DataFoundationError as exc:
        raise BreakoutApplicationError(
            f"{data_universe} 尚无已发布行情版本"
        ) from exc
    if not metadata.empty and "ticker" in metadata.columns:
        metadata["ticker"] = metadata["ticker"].astype(str).str.upper()
        tickers = metadata["ticker"].tolist()
        names = (
            metadata.set_index("ticker")
            .get("name", pd.Series(dtype="object"))
            .fillna("")
            .to_dict()
        )
        sectors = (
            metadata.set_index("ticker")
            .get("sector", pd.Series(dtype="object"))
            .fillna("")
            .to_dict()
        )
        if "current_dollar_volume" in metadata.columns:
            current_dollar_volume = (
                pd.to_numeric(
                    metadata.set_index("ticker")["current_dollar_volume"],
                    errors="coerce",
                )
                .dropna()
                .to_dict()
            )
        else:
            current_dollar_volume = {}
    else:
        tickers, names, sectors, current_dollar_volume = [], {}, {}, {}

    return {
        "universe": universe,
        "label": BREAKOUT_UNIVERSE_LABELS.get(universe, universe),
        "tickers": tickers,
        "names": names,
        "sectors": sectors,
        "current_dollar_volume": current_dollar_volume,
        "data_universe": data_universe,
        "dataset_version_id": published.version.version_id,
    }


def build_breakout_scan(
    *,
    universe: str | None,
    enabled_universes: Iterable[str],
    asof: str | None,
    min_return_20d: float,
    min_adr_20d: float,
    min_dollar_volume_m: float,
    min_avg_dollar_volume_m: float,
    min_consolidation_days: int,
    max_distance_ma50: float,
    pivot_proximity: float,
    market_symbol: str,
    view: str,
    dataset_version_id: str | None = None,
    market_dataset_version_id: str | None = None,
    watchlist_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    context = resolve_breakout_universe(
        universe,
        enabled_universes=enabled_universes,
        dataset_version_id=dataset_version_id,
    )
    if (
        watchlist_snapshot_sha256 is not None
        and context.get("watchlist_snapshot_sha256") != watchlist_snapshot_sha256
    ):
        raise BreakoutApplicationError("股票池已变更，请刷新页面提交当前股票池扫描")

    filters = BreakoutFilters(
        min_return_20d=min_return_20d,
        min_adr_20d=min_adr_20d,
        min_dollar_volume=min_dollar_volume_m * 1_000_000,
        min_avg_dollar_volume=min_avg_dollar_volume_m * 1_000_000,
        min_consolidation_days=min_consolidation_days,
        max_distance_ma50=max_distance_ma50,
        pivot_proximity=pivot_proximity,
    ).normalized()
    total_universe_count = len(context["tickers"])
    scan_tickers = context["tickers"]
    current_liquidity = context.get("current_dollar_volume") or {}
    if current_liquidity and filters.min_dollar_volume > 0:
        scan_tickers = [
            ticker
            for ticker in scan_tickers
            if ticker not in current_liquidity
            or float(current_liquidity[ticker]) >= filters.min_dollar_volume
        ]
    market_ticker = market_symbol if market_symbol in {"QQQ", "IWM"} else "QQQ"
    daily_tickers = list(scan_tickers)
    if context["data_universe"] == US_LIQUID_5M:
        daily_tickers = list(dict.fromkeys([*daily_tickers, market_ticker]))

    try:
        daily = load_breakout_daily_dataset(
            requested_universe=context["universe"],
            data_universe=context["data_universe"],
            tickers=daily_tickers,
            end=asof or None,
            dataset_version_id=context["dataset_version_id"],
            min_latest_coverage=(
                1.0
                if context["universe"].lower().startswith("watchlist:")
                else 0.98
            ),
        )
    except DataFoundationError as exc:
        raise BreakoutApplicationError(
            f"{context['data_universe']} 已发布版本不满足扫描数据契约"
        ) from exc

    scan = scan_breakouts(
        scan_tickers,
        filters=filters,
        asof=asof or None,
        names=context["names"],
        sectors=context["sectors"],
        data_universe=context["data_universe"],
        dataset_version_id=context["dataset_version_id"],
        frames=daily.frames,
    )
    all_rows = scan["rows"]
    normalized_view = view if view in {"all", "setup", "ready", "breakout"} else "all"
    if normalized_view == "setup":
        visible_rows = [row for row in all_rows if row["setup_qualified"]]
    elif normalized_view == "ready":
        visible_rows = [
            row for row in all_rows if row["status"] in {"READY", "BREAKOUT"}
        ]
    elif normalized_view == "breakout":
        visible_rows = [row for row in all_rows if row["status"] == "BREAKOUT"]
    else:
        visible_rows = all_rows

    for row in visible_rows:
        row["checks_passed"] = sum(bool(value) for value in row["setup_checks"].values())

    scan["all_candidate_count"] = scan["candidate_count"]
    scan["liquidity_prefilter_count"] = scan["universe_count"]
    scan["universe_count"] = total_universe_count
    scan["visible_count"] = len(visible_rows)
    scan["rows"] = visible_rows
    scan["universe"] = context["universe"]
    scan["universe_label"] = context["label"]
    scan["data_universe"] = context["data_universe"]
    scan["dataset_version_id"] = context["dataset_version_id"]
    scan["data_contract"] = daily.contract.to_dict()
    scan["view"] = normalized_view
    scan["data_lag_days"] = max(
        0,
        (pd.Timestamp.now().normalize() - pd.Timestamp(scan["asof"])).days,
    )
    if daily.data_universe == US_LIQUID_5M and not daily.frame(market_ticker).empty:
        market_daily = daily
    else:
        selected_market_version = (
            daily.dataset_version_id
            if daily.data_universe == US_LIQUID_5M
            else market_dataset_version_id
        )
        try:
            market_daily = load_breakout_daily_dataset(
                requested_universe="MARKET_REGIME",
                data_universe=US_LIQUID_5M,
                tickers=[market_ticker],
                end=scan["asof"],
                dataset_version_id=selected_market_version,
            )
        except DataFoundationError as exc:
            raise BreakoutApplicationError(
                "市场过滤所需的 QQQ/IWM 发布行情不可用"
            ) from exc
    scan["market"] = load_market_regime(
        asof=scan["asof"],
        symbol=market_ticker,
        fetch_missing=False,
        data_universe=US_LIQUID_5M,
        dataset_version_id=market_daily.dataset_version_id,
        frame=market_daily.frame(market_ticker),
    )
    scan["market_dataset_version_id"] = market_daily.dataset_version_id
    scan["market_data_contract"] = market_daily.contract.to_dict()
    return scan


def get_breakout_scan(
    *,
    universe: str | None,
    enabled_universes: Iterable[str],
    asof: str | None,
    min_return_20d: float,
    min_adr_20d: float,
    min_dollar_volume_m: float,
    min_avg_dollar_volume_m: float,
    min_consolidation_days: int,
    max_distance_ma50: float,
    pivot_proximity: float,
    market_symbol: str,
    view: str,
    force: bool = False,
    allow_build: bool = True,
) -> dict[str, Any]:
    """Return a cached scan or optionally build it under a process-local lock.

    Web requests must pass ``allow_build=False``. A broad-universe cache miss can
    materialize hundreds of trading days for thousands of securities, so that
    work belongs to a resource-bounded background service rather than a Web
    worker.
    """
    normalized_universe = normalize_breakout_universe(universe)
    normalized_enabled = _normalized_enabled_universes(enabled_universes)
    context = resolve_breakout_universe(
        normalized_universe,
        enabled_universes=normalized_enabled,
    )
    try:
        market_version_id = (
            context["dataset_version_id"]
            if context["data_universe"] == US_LIQUID_5M
            else resolve_published_version(
                requested_universe="MARKET_REGIME",
                data_universe=US_LIQUID_5M,
            ).version_id
        )
    except DataFoundationError as exc:
        raise BreakoutApplicationError(
            "市场过滤所需的 QQQ/IWM 发布行情不可用"
        ) from exc
    parameters = {
        "universe": normalized_universe,
        "asof": asof or "",
        "min_return_20d": float(min_return_20d),
        "min_adr_20d": float(min_adr_20d),
        "min_dollar_volume_m": float(min_dollar_volume_m),
        "min_avg_dollar_volume_m": float(min_avg_dollar_volume_m),
        "min_consolidation_days": int(min_consolidation_days),
        "max_distance_ma50": float(max_distance_ma50),
        "pivot_proximity": float(pivot_proximity),
        "market_symbol": str(market_symbol).upper(),
        "view": view if view in {"all", "setup", "ready", "breakout"} else "all",
        "dataset_version_id": context["dataset_version_id"],
        "market_dataset_version_id": market_version_id,
    }
    if context.get("watchlist_snapshot_sha256"):
        parameters["watchlist_snapshot_sha256"] = context["watchlist_snapshot_sha256"]
    if not force:
        cached = load_scan_cache(parameters)
        if cached is not None:
            return cached

    if not allow_build:
        job = request_scan_build(
            parameters, enabled_universes=normalized_enabled, force=force,
        )
        if job["status"] == "FAILED" and job["attempts"] >= 3:
            raise BreakoutScanNotReadyError(
                f"后台扫描失败：{job.get('error', '')}；请检查后台任务后重试"
            )
        raise BreakoutScanNotReadyError(
            "扫描已提交后台任务，请稍后刷新页面读取结果"
        )

    with _SCAN_LOCK:
        if not force:
            cached = load_scan_cache(parameters)
            if cached is not None:
                return cached
        scan = build_breakout_scan(
            **parameters,
            enabled_universes=normalized_enabled,
        )
        save_scan_cache(parameters, scan)
        return scan


def process_pending_scan_requests(*, limit: int = 1) -> list[dict[str, Any]]:
    return process_scan_build_requests(build_breakout_scan, limit=limit)


__all__ = [
    "BREAKOUT_UNIVERSE_LABELS",
    "BreakoutApplicationError",
    "BreakoutScanNotReadyError",
    "BreakoutWatchlistNotFoundError",
    "UnknownBreakoutUniverseError",
    "breakout_universe_options",
    "build_breakout_scan",
    "get_breakout_scan",
    "process_pending_scan_requests",
    "normalize_breakout_universe",
    "resolve_breakout_universe",
]
