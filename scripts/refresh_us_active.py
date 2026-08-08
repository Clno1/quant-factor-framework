#!/usr/bin/env python3
"""Publish the versioned US_LIQUID_5M universe after the US market close."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "data" / "cache" / "matplotlib"))

from src.alerts.config import AlertSettings, load_local_env  # noqa: E402
from src.config import CONFIG  # noqa: E402
from src.data.foundation import MarketDataReader, MarketDataWriter  # noqa: E402
from src.data.universe import get_universe  # noqa: E402
from src.data.universe_ids import US_LIQUID_5M  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402


def _latest_completed_xnys_session(
    *,
    now: pd.Timestamp | None = None,
    calendar=None,
) -> pd.Timestamp:
    """Return the latest XNYS session whose official close is at least 90m old."""
    now_utc = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize("UTC")
    else:
        now_utc = now_utc.tz_convert("UTC")
    if calendar is None:
        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise RuntimeError(
                "exchange_calendars is required for XNYS refresh targeting"
            ) from exc
        calendar = xcals.get_calendar("XNYS")
    sessions = calendar.sessions_in_range(
        (now_utc - pd.Timedelta(days=20)).date(),
        now_utc.date(),
    )
    cutoff = now_utc - pd.Timedelta(minutes=90)
    completed: list[pd.Timestamp] = []
    for session in sessions:
        close = pd.Timestamp(calendar.session_close(session))
        close = (
            close.tz_localize("UTC")
            if close.tzinfo is None
            else close.tz_convert("UTC")
        )
        if close <= cutoff:
            completed.append(pd.Timestamp(session).tz_localize(None).normalize())
    if not completed:
        raise RuntimeError("no completed XNYS session was found in the lookback window")
    return completed[-1]


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _publish_universe_manifest(
    universe: pd.DataFrame,
    *,
    source_session: pd.Timestamp,
    refresh_started_at: datetime,
    previous_signature: tuple[int, int, int] | None,
) -> Path:
    cache_path = ROOT / "data" / "raw" / "universe" / "us_active.parquet"
    if not cache_path.is_file():
        raise RuntimeError("forced US_ACTIVE refresh did not produce its parquet cache")
    stat = cache_path.stat()
    current_signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    if previous_signature == current_signature:
        raise RuntimeError(
            "forced US_ACTIVE refresh reused the previous cache; manifest not published"
        )
    if stat.st_mtime < refresh_started_at.timestamp() - 2.0:
        raise RuntimeError(
            "forced US_ACTIVE refresh fell back to a stale cache; manifest not published"
        )
    manifest_path = cache_path.with_suffix(".premarket.json")
    atomic_save_json(
        {
            "schema_version": 1,
            "universe": "US_ACTIVE",
            "source_session": source_session.date().isoformat(),
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "parquet_sha256": _sha256_path(cache_path),
            "row_count": len(universe),
        },
        manifest_path,
    )
    return manifest_path


def _select_refresh_tickers(
    universe: pd.DataFrame,
    *,
    stocks_only: bool,
    liquidity_floor: float,
    always_tickers: set[str],
    limit: int | None,
) -> list[str]:
    if "ticker" not in universe.columns:
        raise RuntimeError("US_ACTIVE is missing ticker; refusing an unsafe refresh")
    selected = universe.loc[universe["ticker"].notna()].copy()
    selected["ticker"] = selected["ticker"].astype(str).str.strip().str.upper()
    selected = selected.loc[selected["ticker"].ne("")].copy()
    if stocks_only:
        if "asset_type" not in selected.columns:
            raise RuntimeError(
                "US_ACTIVE is missing asset_type; refusing an unsafe stocks-only refresh"
            )
        selected = selected[
            selected["asset_type"].fillna("").astype(str).str.upper().eq("STOCK")
        ].copy()
    if liquidity_floor > 0 and "current_dollar_volume" not in selected.columns:
        raise RuntimeError(
            "US_ACTIVE is missing current_dollar_volume; refusing an unsafe liquidity filter"
        )
    if liquidity_floor > 0:
        selected = selected[
            (
                pd.to_numeric(selected["current_dollar_volume"], errors="coerce")
                >= liquidity_floor
            )
            | selected["ticker"].isin(always_tickers)
        ]
    tickers = selected["ticker"].drop_duplicates().tolist()
    if limit is not None:
        ordinary = [ticker for ticker in tickers if ticker not in always_tickers]
        forced = [ticker for ticker in tickers if ticker in always_tickers]
        tickers = list(dict.fromkeys([*ordinary[: max(1, limit)], *forced]))
    return tickers


def _foundation_setting(name: str, default):
    try:
        return getattr(CONFIG.data.foundation, name)
    except (AttributeError, KeyError):
        return default


def _liquid_setting(name: str, default):
    try:
        return getattr(CONFIG.data.liquid_universe, name)
    except (AttributeError, KeyError):
        return default


def _initial_start(target: pd.Timestamp) -> pd.Timestamp:
    raw = str(_liquid_setting("initial_start", "180D")).strip().upper()
    if raw.endswith("D") and raw[:-1].isdigit():
        return target - pd.Timedelta(days=int(raw[:-1]))
    return pd.Timestamp(raw).normalize()


def _formal_universe_frame(
    source: pd.DataFrame,
    *,
    tickers: list[str],
    target: pd.Timestamp,
) -> pd.DataFrame:
    metadata = source.copy()
    metadata["ticker"] = (
        metadata["ticker"].astype(str).str.strip().str.upper()
    )
    metadata = (
        metadata.loc[metadata["ticker"].isin(tickers)]
        .drop_duplicates("ticker", keep="last")
        .copy()
    )
    observed = set(metadata["ticker"])
    missing = [ticker for ticker in tickers if ticker not in observed]
    if missing:
        metadata = pd.concat(
            [
                metadata,
                pd.DataFrame(
                    {
                        "ticker": missing,
                        "name": missing,
                        "sector": None,
                        "sub_industry": None,
                        "asset_type": "BENCHMARK",
                    }
                ),
            ],
            ignore_index=True,
        )
    metadata["selection_date"] = target
    metadata["selection_rule"] = "current_dollar_volume"
    return metadata.reset_index(drop=True)


def _versioned_membership(
    reader: MarketDataReader,
    *,
    tickers: list[str],
    start: pd.Timestamp,
    target: pd.Timestamp,
) -> pd.DataFrame:
    try:
        existing = reader.load_membership(US_LIQUID_5M)
    except Exception:
        existing = None
    snapshots: list[pd.DataFrame] = []
    if existing is not None and not existing.empty:
        snapshots.append(existing.loc[existing["date"].lt(target)].copy())
    else:
        snapshots.append(
            pd.DataFrame({"date": start, "ticker": tickers, "active": True})
        )
    snapshots.append(
        pd.DataFrame({"date": target, "ticker": tickers, "active": True})
    )
    return (
        pd.concat(snapshots, ignore_index=True)
        .drop_duplicates(["date", "ticker"], keep="last")
        .sort_values(["date", "ticker"])
        .reset_index(drop=True)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--min-current-dollar-volume-m",
        type=float,
        default=0.0,
        help="Only refresh symbols whose screener dollar volume meets this USD millions floor.",
    )
    parser.add_argument("--force-universe", action="store_true")
    parser.add_argument(
        "--stocks-only",
        action="store_true",
        help="Refresh only rows classified as STOCK; intended for the default alert server.",
    )
    parser.add_argument(
        "--market-symbol",
        action="append",
        default=[],
        help="Also refresh a market-regime benchmark (default: QQQ); repeatable.",
    )
    parser.add_argument("--skip-precompute", action="store_true")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Safely load KEY=VALUE settings without shell-sourcing the file.",
    )
    args = parser.parse_args()
    if args.env_file is not None:
        if load_local_env(args.env_file) is None:
            raise FileNotFoundError("the requested environment file does not exist")

    cache_path = ROOT / "data" / "raw" / "universe" / "us_active.parquet"
    previous_signature = None
    if cache_path.is_file():
        previous_stat = cache_path.stat()
        previous_signature = (
            previous_stat.st_mtime_ns,
            previous_stat.st_size,
            previous_stat.st_ino,
        )
    target = _latest_completed_xnys_session()
    refresh_started_at = datetime.now(timezone.utc)
    universe = get_universe("US_ACTIVE", force_refresh=args.force_universe)
    if args.force_universe:
        manifest_path = _publish_universe_manifest(
            universe,
            source_session=target,
            refresh_started_at=refresh_started_at,
            previous_signature=previous_signature,
        )
        print(f"published_universe_manifest={manifest_path}")
    configured_floor = float(
        _liquid_setting("min_current_dollar_volume_m", 5.0)
    )
    requested_floor = (
        args.min_current_dollar_volume_m
        if args.min_current_dollar_volume_m > 0
        else configured_floor
    )
    liquidity_floor = max(0.0, requested_floor) * 1_000_000
    always_tickers = set(
        AlertSettings.load(
            load_env=False,
            # This unit reads momentum-alerts.env and must continue refreshing
            # legacy hourly-only extras during their config migration.
            include_environment_tickers=True,
        ).always_tickers
    )
    stocks_only = args.stocks_only or (
        str(_liquid_setting("asset_type", "STOCK")).strip().upper() == "STOCK"
    )
    tickers = _select_refresh_tickers(
        universe,
        stocks_only=stocks_only,
        liquidity_floor=liquidity_floor,
        always_tickers=always_tickers,
        limit=args.limit,
    )
    market_symbols = [
        symbol.strip().upper()
        for value in (args.market_symbol or ["QQQ"])
        for symbol in value.split(",")
        if symbol.strip()
    ]
    support_symbols = [
        str(symbol).strip().upper()
        for symbol in _liquid_setting("always_tickers", ["QQQ", "SPY", "IWM"])
        if str(symbol).strip()
    ]
    tickers = list(
        dict.fromkeys([*tickers, *support_symbols, *market_symbols])
    )
    print(
        f"{US_LIQUID_5M} publish: {len(tickers)} symbols, target={target.date()}, "
        f"workers={args.workers}, assets={'stocks' if stocks_only else 'stocks+etfs'}, "
        f"liquidity_floor=${liquidity_floor / 1_000_000:.1f}M, "
        f"always_tickers={len(always_tickers)}, "
        f"market_symbols={','.join(market_symbols)}"
    )

    start = _initial_start(target)
    reader = MarketDataReader()
    membership = _versioned_membership(
        reader,
        tickers=tickers,
        start=start,
        target=target,
    )
    formal_universe = _formal_universe_frame(
        universe,
        tickers=tickers,
        target=target,
    )
    writer = MarketDataWriter(catalog=reader.catalog)
    result = writer.update_universe(
        US_LIQUID_5M,
        target_session=target,
        force=args.force_universe,
        workers=args.workers,
        universe_frame=formal_universe,
        initial_start=start,
        membership_frame=membership,
        membership_source=f"US_ACTIVE_liquidity_snapshot:{target.date()}",
        min_latest_coverage=float(
            _foundation_setting("min_latest_coverage", 0.98)
        ),
    )
    version = result.version
    if version is None:
        raise RuntimeError(f"{US_LIQUID_5M} publication returned no version")
    print(
        f"published_version={version.version_id} status={result.status} "
        f"rows={version.row_count} tickers={version.ticker_count} "
        f"coverage={version.target_coverage:.2%} "
        f"failed_fetches={len(result.failed_tickers)}"
    )
    if result.failed_tickers:
        print("failed_tickers=" + ",".join(result.failed_tickers[:100]))
    if args.limit is None:
        from src.breakouts.scan_cache import clear_scan_cache

        removed = clear_scan_cache()
        print(f"cleared_scan_cache={removed}")
        if not args.skip_precompute:
            from src.breakouts.application import get_breakout_scan

            print("precomputing default momentum scan ...")
            scan = get_breakout_scan(
                universe="US_ACTIVE",
                enabled_universes=("US_ACTIVE",),
                asof=None,
                min_return_20d=20.0,
                min_adr_20d=6.0,
                min_dollar_volume_m=10.0,
                min_avg_dollar_volume_m=10.0,
                min_consolidation_days=9,
                max_distance_ma50=35.0,
                pivot_proximity=3.0,
                market_symbol="QQQ",
                view="all",
                force=True,
            )
            print(
                "precomputed "
                f"universe={scan['universe_count']} candidates={scan['candidate_count']} asof={scan['asof']}"
            )


if __name__ == "__main__":
    main()
