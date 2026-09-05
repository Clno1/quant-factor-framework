#!/usr/bin/env python3
"""Incrementally publish US_EQUITY_COVERAGE from FMP dated EOD bulk rows.

FMP documents EOD bulk as one dated market-wide ingestion per trading day.
Each update also downloads a fresh overlap to authenticate price/volume units.
Corporate-action scale changes rebase older monthly partitions before appending
new sessions. Month-end nominal close is sourced separately for PIT selection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sys
import tempfile
import time
from uuid import uuid4

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.data.broad_coverage import (  # noqa: E402
    BAR_QUARANTINE_COLUMNS,
    BroadCoverageReader,
    BroadCoverageStore,
    coverage_bar_quarantine_checks,
    fetch_coverage_history_delta,
    map_eod_bulk_to_security_ids,
    normalize_coverage_bars,
    select_coverage_securities,
    split_coverage_bar_quality,
)
from src.data.fmp import (  # noqa: E402
    get_coverage_historical_ohlcv, get_eod_bulk, get_unadjusted_historical_close,
)
from src.data.price_semantics import build_price_semantics_contract  # noqa: E402
from src.data.foundation import (  # noqa: E402
    DataFoundationError,
    MarketDataCatalog,
    MarketDataReader,
    QualityCheck,
    _rebase_parent_to_fetched_scale,
)
from src.data.security_master_store import SecurityMasterStore  # noqa: E402
from src.data.universe_ids import US_EQUITY_COVERAGE  # noqa: E402
from src.utils.env import load_local_env  # noqa: E402
from src.utils.file_lock import file_lock  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402
from src.utils.market_calendar import latest_publishable_xnys_session  # noqa: E402


PROVIDER_CACHE_SCHEMA_VERSION = 1
PROVIDER_CACHE_METHOD = "FMP_COVERAGE_PROVIDER_CACHE_V3_RECONCILED"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-session")
    parser.add_argument(
        "--overlap-calendar-days",
        type=int,
        default=int(CONFIG.data.foundation.overlap_calendar_days),
        help=(
            "Fresh historical overlap used to authenticate the parent's price "
            "and volume scales before publishing an incremental version."
        ),
    )
    parser.add_argument(
        "--batch-size", type=int, default=int(CONFIG.data.broad_coverage.shard_size)
    )
    parser.add_argument("--env-file")
    parser.add_argument(
        "--output-dir", default="data/lake/staging/us_equity_coverage_incremental"
    )
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return float(value) / divisor


def _stable_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_provider_cache(
    *,
    output_dir: str | Path,
    target: pd.Timestamp,
    refresh_start: pd.Timestamp,
    refresh_sessions: pd.DatetimeIndex,
    history_start: str,
    parent_version_id: str,
    security_master_generation_id: str,
    security_master_manifest_sha256: str,
) -> tuple[Path, dict, str]:
    contract = {
        "schema_version": PROVIDER_CACHE_SCHEMA_VERSION,
        "method": PROVIDER_CACHE_METHOD,
        "target_session": target.date().isoformat(),
        "refresh_start": refresh_start.date().isoformat(),
        "history_start": str(history_start),
        "sessions": [value.date().isoformat() for value in refresh_sessions],
        "parent_dataset_version_id": parent_version_id,
        "security_master_generation_id": security_master_generation_id,
        "security_master_manifest_sha256": security_master_manifest_sha256,
    }
    fingerprint = _stable_sha256(contract)
    cache_dir = (
        Path(output_dir).resolve()
        / f"asof={target.date()}"
        / "provider_cache"
        / f"binding={fingerprint}"
    )
    cache_dir.mkdir(parents=True, exist_ok=True)
    contract_path = cache_dir / "contract.json"
    if contract_path.exists():
        observed = json.loads(contract_path.read_text(encoding="utf-8"))
        if observed != contract:
            raise DataFoundationError(
                f"provider cache contract mismatch: {contract_path}"
            )
    else:
        atomic_save_json(contract, contract_path)
    return cache_dir, contract, fingerprint


def _validate_cached_history_delta(
    frame: pd.DataFrame,
    *,
    security_ids: list[str],
    history_start: pd.Timestamp,
    history_end: pd.Timestamp,
) -> pd.DataFrame:
    required = {
        "date", "security_id", "ticker", "open", "high", "low", "close",
        "adj_close", "volume",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(
            f"cached identity history delta is missing columns: {missing}"
        )
    normalized = frame.copy()
    normalized["date"] = pd.to_datetime(
        normalized["date"], errors="coerce"
    ).dt.normalize()
    if normalized["date"].isna().any():
        raise DataFoundationError("cached identity history delta has invalid dates")
    if (
        normalized["date"].lt(history_start).any()
        or normalized["date"].gt(history_end).any()
    ):
        raise DataFoundationError(
            "cached identity history delta is outside the requested range"
        )
    unexpected = sorted(
        set(normalized["security_id"].astype(str)) - set(security_ids)
    )
    if unexpected:
        raise DataFoundationError(
            f"cached identity history delta has unexpected identities: {unexpected[:20]}"
        )
    if normalized.duplicated(["date", "security_id"]).any():
        raise DataFoundationError(
            "cached identity history delta has duplicate date/security keys"
        )
    return normalized.sort_values(["date", "security_id"]).reset_index(drop=True)


def _load_or_fetch_history_delta(
    *,
    cache_dir: Path,
    security_universe: pd.DataFrame,
    symbol_history: pd.DataFrame,
    security_ids: list[str],
    history_start: str | pd.Timestamp,
    history_end: pd.Timestamp,
    fetcher=get_coverage_historical_ohlcv,
) -> tuple[pd.DataFrame, list[dict], list[dict], bool]:
    if not security_ids:
        return pd.DataFrame(), [], [], False
    requested_start = pd.Timestamp(history_start).normalize()
    destination = cache_dir / "identity_delta"
    frame_path = destination / "frame.parquet"
    manifest_path = destination / "manifest.json"
    if destination.exists():
        if not frame_path.is_file() or not manifest_path.is_file():
            raise DataFoundationError(
                f"incomplete identity history cache: {destination}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema_version") != PROVIDER_CACHE_SCHEMA_VERSION
            or manifest.get("method") != PROVIDER_CACHE_METHOD
            or manifest.get("security_ids") != security_ids
            or manifest.get("history_start") != requested_start.date().isoformat()
            or manifest.get("history_end") != history_end.date().isoformat()
            or manifest.get("frame_sha256") != _sha256(frame_path)
            or manifest.get("failures")
        ):
            raise DataFoundationError(
                f"identity history cache manifest mismatch: {destination}"
            )
        frame = _validate_cached_history_delta(
            pd.read_parquet(frame_path),
            security_ids=security_ids,
            history_start=requested_start,
            history_end=history_end,
        )
        if int(manifest.get("row_count", -1)) != len(frame):
            raise DataFoundationError(
                f"identity history cache row count mismatch: {destination}"
            )
        return frame, [], list(manifest.get("fallbacks") or []), True

    frame, failures, fallbacks = fetch_coverage_history_delta(
        security_universe,
        symbol_history,
        security_ids=security_ids,
        history_start=history_start,
        target_session=history_end,
        fetcher=fetcher,
    )
    if failures:
        return frame, failures, fallbacks, False
    frame = _validate_cached_history_delta(
        frame,
        security_ids=security_ids,
        history_start=requested_start,
        history_end=history_end,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=".tmp_identity_delta_",
        dir=destination.parent,
    ))
    try:
        temporary_frame = temporary / "frame.parquet"
        frame.to_parquet(
            temporary_frame,
            index=False,
            compression="snappy",
        )
        manifest = {
            "schema_version": PROVIDER_CACHE_SCHEMA_VERSION,
            "method": PROVIDER_CACHE_METHOD,
            "security_ids": security_ids,
            "history_start": requested_start.date().isoformat(),
            "history_end": history_end.date().isoformat(),
            "row_count": len(frame),
            "failures": [],
            "fallbacks": fallbacks,
            "frame_sha256": _sha256(temporary_frame),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_save_json(manifest, temporary / "manifest.json")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return frame, [], fallbacks, False


def _validate_cached_eod_bulk(
    frame: pd.DataFrame,
    *,
    session: pd.Timestamp,
) -> pd.DataFrame:
    required = {
        "date", "ticker", "open", "high", "low", "close", "adj_close", "volume"
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(
            f"cached FMP EOD bulk is missing columns: {missing}"
        )
    normalized = frame.copy()
    normalized["date"] = pd.to_datetime(
        normalized["date"], errors="coerce"
    ).dt.normalize()
    if normalized.empty or normalized["date"].isna().any():
        raise DataFoundationError(
            f"cached FMP EOD bulk is empty or has invalid dates: {session.date()}"
        )
    if not normalized["date"].eq(session).all():
        raise DataFoundationError(
            f"cached FMP EOD bulk contains rows outside {session.date()}"
        )
    if normalized["ticker"].fillna("").astype(str).str.strip().eq("").any():
        raise DataFoundationError(
            f"cached FMP EOD bulk contains an empty ticker: {session.date()}"
        )
    if normalized.duplicated(["date", "ticker"]).any():
        raise DataFoundationError(
            f"cached FMP EOD bulk contains duplicate tickers: {session.date()}"
        )
    return normalized.sort_values("ticker").reset_index(drop=True)


def _load_or_fetch_eod_bulk_session(
    *,
    cache_dir: Path,
    session: pd.Timestamp,
    fetcher=get_eod_bulk,
) -> tuple[pd.DataFrame, bool, dict]:
    session_text = session.date().isoformat()
    destination = cache_dir / "eod" / f"session={session_text}"
    frame_path = destination / "frame.parquet"
    manifest_path = destination / "manifest.json"
    if destination.exists():
        if not frame_path.is_file() or not manifest_path.is_file():
            raise DataFoundationError(
                f"incomplete FMP EOD bulk cache: {destination}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema_version") != PROVIDER_CACHE_SCHEMA_VERSION
            or manifest.get("method") != PROVIDER_CACHE_METHOD
            or manifest.get("session") != session_text
            or manifest.get("frame_sha256") != _sha256(frame_path)
        ):
            raise DataFoundationError(
                f"FMP EOD bulk cache manifest mismatch: {destination}"
            )
        frame = _validate_cached_eod_bulk(
            pd.read_parquet(frame_path),
            session=session,
        )
        if int(manifest.get("row_count", -1)) != len(frame):
            raise DataFoundationError(
                f"FMP EOD bulk cache row count mismatch: {destination}"
            )
        frame.attrs["invalid_ticker_rows"] = int(
            manifest.get("invalid_ticker_rows", 0)
        )
        return frame, True, manifest

    frame = _validate_cached_eod_bulk(fetcher(session), session=session)
    invalid_ticker_rows = int(frame.attrs.get("invalid_ticker_rows", 0))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".tmp_session={session_text}_",
        dir=destination.parent,
    ))
    try:
        temporary_frame = temporary / "frame.parquet"
        frame.to_parquet(
            temporary_frame,
            index=False,
            compression="snappy",
        )
        manifest = {
            "schema_version": PROVIDER_CACHE_SCHEMA_VERSION,
            "method": PROVIDER_CACHE_METHOD,
            "session": session_text,
            "row_count": len(frame),
            "invalid_ticker_rows": invalid_ticker_rows,
            "frame_sha256": _sha256(temporary_frame),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_save_json(manifest, temporary / "manifest.json")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    frame.attrs["invalid_ticker_rows"] = invalid_ticker_rows
    return frame, False, manifest


def _sessions(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    values = xcals.get_calendar("XNYS").sessions_in_range(start, end)
    if values.tz is not None:
        values = values.tz_localize(None)
    return pd.DatetimeIndex(values).normalize()


def _sessions_after_parent(
    parent_target: str | pd.Timestamp,
    target: str | pd.Timestamp,
) -> pd.DatetimeIndex:
    """Return only unpublished XNYS sessions after an immutable parent."""
    parent = pd.Timestamp(parent_target).normalize()
    requested = pd.Timestamp(target).normalize()
    if requested <= parent:
        return pd.DatetimeIndex([])
    return _sessions(parent + pd.Timedelta(days=1), requested)


def _parent_partition_paths(
    parent: object,
    *,
    affected_months: set[str],
) -> tuple[list[Path], list[Path], set[str]]:
    index_path = Path(str(parent.bars_path))
    if not index_path.is_absolute():
        index_path = ROOT / index_path
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    if payload.get("storage_type") != "PARTITIONED_PARQUET_V1":
        raise DataFoundationError(
            "incremental broad coverage requires a partitioned parent version"
        )
    unchanged: list[Path] = []
    replaced: list[Path] = []
    # The target may be in a brand-new month that has no parent partition yet.
    # Start with every affected month, then widen only when a legacy annual
    # partition intersects and therefore has to be split in full.
    rebuild_months: set[str] = set(affected_months)
    for entry in payload.get("partitions") or []:
        path = (index_path.parent / str(entry["file"])).resolve()
        periods = {
            str(period)
            for period in pd.period_range(
                pd.Timestamp(entry["min_date"]),
                pd.Timestamp(entry["max_date"]),
                freq="M",
            )
        }
        if periods & affected_months:
            replaced.append(path)
            # A legacy annual partition must be rebuilt in full the first time
            # it intersects an affected month.  The resulting publication is
            # monthly, so later runs replace only the truly affected months.
            rebuild_months.update(periods)
        else:
            unchanged.append(path)
    if not unchanged and not replaced:
        raise DataFoundationError("parent coverage partition index is empty")
    return unchanged, replaced, rebuild_months


def _coverage_rebase_audit(
    previous_overlap: pd.DataFrame,
    fresh: pd.DataFrame,
    *,
    parent_security_ids: set[str],
) -> list[dict]:
    """Authenticate each continuing stable identity, independently of its alias."""
    continuing = set(fresh["security_id"].astype(str)) & parent_security_ids
    missing = continuing - set(previous_overlap["security_id"].astype(str))
    if missing:
        raise DataFoundationError(
            f"continuing securities lack an overlap anchor: {sorted(missing)[:20]}; "
            "extend overlap or rebuild coverage"
        )
    previous = previous_overlap.copy()
    current = fresh.loc[fresh["security_id"].astype(str).isin(continuing)].copy()
    # A ticker can change or be reused. Scale authentication uses security_id.
    previous["ticker"] = previous["security_id"].astype(str)
    current["ticker"] = current["security_id"].astype(str)
    # The shared authenticator scans its parent table per ticker. Partition it
    # first so broad coverage does not perform a whole-pool scan for every name.
    parent_groups = previous.groupby("ticker", sort=False).indices
    audit = []
    for security_id, fresh_rows in current.groupby("ticker", sort=False):
        _, rows = _rebase_parent_to_fetched_scale(
            previous.iloc[parent_groups[security_id]], fresh_rows,
        )
        for row in rows:
            # This helper only sees the overlap. The writer subsequently scales
            # every retained parent month, so its old-row count is inapplicable.
            row.pop("older_rows_rebased", None)
            audit.append({**row, "security_id": security_id})
    return audit


def _rebase_coverage_partition(previous: pd.DataFrame, audit: list[dict]) -> pd.DataFrame:
    """Scale an old monthly partition; nominal historical prices never change."""
    out = previous.copy()
    if out.empty:
        return out
    for field in ("open", "high", "low", "close", "adj_close", "volume"):
        factors = {row["security_id"]: row["scales"][field] for row in audit}
        scale = out["security_id"].astype(str).map(factors).fillna(1.)
        out[field] = pd.to_numeric(out[field], errors="coerce") * scale
    return out


def _attach_month_end_nominal_close(
    frame: pd.DataFrame,
    *,
    after: pd.Timestamp,
    target: pd.Timestamp,
    fetcher=get_unadjusted_historical_close,
) -> pd.DataFrame:
    """Fetch original dollar prices only for newly completed month-end snapshots."""
    out = frame.copy()
    future = _sessions(after + pd.Timedelta(days=1), target + pd.Timedelta(days=40))
    month_ends = pd.Series(future, index=future.to_period("M")).groupby(level=0).max()
    month_ends = month_ends.loc[month_ends.le(target)]
    needed = out["date"].isin(month_ends)
    for ticker, rows in out.loc[needed].groupby("ticker", sort=False):
        nominal = fetcher(str(ticker), str(rows.date.min().date()), str(rows.date.max().date()))
        prices = rows["date"].map(nominal)
        if prices.isna().any() or not np.isfinite(prices).all() or prices.le(0).any():
            raise DataFoundationError(f"{ticker}: incomplete month-end unadjusted prices")
        out.loc[rows.index, "unadjusted_close"] = prices
    return out


def run(args: argparse.Namespace) -> tuple[dict, int]:
    started = time.perf_counter()
    target = (
        pd.Timestamp(args.target_session).normalize()
        if args.target_session
        else pd.Timestamp(latest_publishable_xnys_session()).normalize()
    )
    catalog = MarketDataCatalog(CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)))
    market_reader = MarketDataReader(catalog=catalog)
    parent = market_reader.require_latest(
        US_EQUITY_COVERAGE,
        require_price_semantics=True,
    )
    parent_manifest = market_reader.verify_version(
        parent,
        require_price_semantics=True,
    )
    parent_target = pd.Timestamp(parent.target_session).normalize()
    if parent_target > target:
        raise DataFoundationError("target session predates the published coverage version")
    if int(args.overlap_calendar_days) < 1:
        raise ValueError("overlap-calendar-days must be positive")
    target_sessions = _sessions(target, target)
    if target_sessions.empty or target_sessions[-1] != target:
        raise DataFoundationError("target session is not an XNYS trading session")

    security_settings = CONFIG.data.security_master
    security_store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(security_settings.snapshot_dir)),
    )
    security_generation, security_frames = security_store.load_published()
    if target.date() > security_generation.target_session:
        raise DataFoundationError(
            "target session exceeds the published Security Master generation"
        )
    if parent_target == target:
        if (
            parent_manifest.get("security_master_generation_id")
            != security_generation.generation_id
            or parent_manifest.get("security_master_manifest_sha256")
            != security_generation.manifest_sha256
        ):
            raise DataFoundationError(
                "same-session coverage is bound to a different Security Master; "
                "run an explicit repair instead of silently reusing it"
            )
        return {
            "status": "NOOP",
            "target_session": target.date().isoformat(),
            "parent_dataset_version_id": parent.version_id,
            "message": "coverage is already published for the target session",
        }, 0
    new_sessions = _sessions_after_parent(parent_target, target)
    if new_sessions.empty or new_sessions[-1] != target:
        raise DataFoundationError(
            "no complete XNYS sessions exist after the parent coverage version"
        )
    refresh_start = max(
        pd.Timestamp(parent.min_date),
        parent_target - pd.Timedelta(days=int(args.overlap_calendar_days)),
    )
    refresh_sessions = _sessions(refresh_start, target)
    refresh_start = refresh_sessions[0]
    settings = CONFIG.data.broad_coverage
    security_universe = select_coverage_securities(
        security_frames["master"],
        history_start=str(settings.history_start),
        target_session=target,
        allowed_asset_types=list(settings.allowed_asset_types),
        benchmark_tickers=list(settings.benchmark_tickers),
        history_policy=security_frames.get("history_policy"),
    )
    security_ids = security_universe["security_id"].astype(str).tolist()
    security_id_set = set(security_ids)
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid4().hex[:8]
    )
    run_dir = (
        Path(args.output_dir).resolve()
        / f"asof={target.date()}"
        / f"run={run_id}"
    )
    run_dir.mkdir(parents=True)
    parent_paths, _none_replaced, _none_rebuilt = _parent_partition_paths(
        parent,
        affected_months=set(),
    )
    connection = catalog._connect(read_only=True)
    try:
        parent_presence_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT security_id FROM read_parquet(?, hive_partitioning=false, union_by_name=true)",
                [[str(path) for path in parent_paths]],
            ).fetchall()
        }
    finally:
        connection.close()
    identity_delta_ids = sorted(security_id_set - parent_presence_ids)
    history_delta_end = parent_target
    provider_cache_dir, _provider_cache_contract, provider_cache_binding = (
        _prepare_provider_cache(
            output_dir=args.output_dir,
            target=target,
            refresh_start=refresh_start,
            refresh_sessions=refresh_sessions,
            history_start=str(settings.history_start),
            parent_version_id=parent.version_id,
            security_master_generation_id=security_generation.generation_id,
            security_master_manifest_sha256=security_generation.manifest_sha256,
        )
    )
    (
        history_delta,
        alias_failures,
        alias_fallbacks,
        history_cache_hit,
    ) = _load_or_fetch_history_delta(
        cache_dir=provider_cache_dir,
        security_universe=security_universe,
        symbol_history=security_frames["symbols"],
        security_ids=identity_delta_ids,
        history_start=str(settings.history_start),
        history_end=history_delta_end,
    )
    security_master_rebase = (
        parent_manifest.get("security_master_generation_id")
        != security_generation.generation_id
        or parent_manifest.get("security_master_manifest_sha256")
        != security_generation.manifest_sha256
    )
    identity_delta_audit = {
        "schema_version": 1,
        "parent_dataset_version_id": parent.version_id,
        "parent_security_master_generation_id": parent_manifest.get(
            "security_master_generation_id"
        ),
        "security_master_generation_id": security_generation.generation_id,
        "security_master_rebase": security_master_rebase,
        "identity_delta_security_ids": identity_delta_ids,
        "identity_delta_security_count": len(identity_delta_ids),
        "history_delta_end": history_delta_end.date().isoformat(),
        "recent_bulk_start": refresh_start.date().isoformat(),
        "history_delta_rows": len(history_delta),
        "history_cache_hit": history_cache_hit,
        "provider_cache_binding": provider_cache_binding,
        "provider_cache_dir": str(provider_cache_dir),
        "alias_failures": alias_failures,
        "alias_fallbacks": alias_fallbacks,
    }
    identity_delta_audit_path = run_dir / "identity_delta_audit.json"
    atomic_save_json(identity_delta_audit, identity_delta_audit_path)
    if alias_failures:
        failed_ids = sorted({str(item["security_id"]) for item in alias_failures})
        raise DataFoundationError(
            "Security Master identity delta has incomplete historical aliases; "
            f"failed_security_ids={failed_ids[:20]} "
            f"audit={identity_delta_audit_path}"
        )
    bulk_frames: list[pd.DataFrame] = []
    invalid_identity_rows_by_session: dict[str, int] = {}
    bulk_cache_hits = 0
    bulk_provider_fetches = 0
    fmp_settings = getattr(CONFIG.data, "fmp", None)
    request_interval = float(
        getattr(fmp_settings, "bulk_request_interval_seconds", 10.0) or 0.0
    )
    last_provider_fetch_finished: float | None = (
        time.monotonic()
        if identity_delta_ids and not history_cache_hit
        else None
    )
    for session in refresh_sessions:
        cached_session = (
            provider_cache_dir / "eod" / f"session={session.date().isoformat()}"
        ).exists()
        if not cached_session and last_provider_fetch_finished is not None:
            remaining = request_interval - (
                time.monotonic() - last_provider_fetch_finished
            )
            if remaining > 0:
                time.sleep(remaining)
        frame, cache_hit, _cache_manifest = _load_or_fetch_eod_bulk_session(
            cache_dir=provider_cache_dir,
            session=session,
        )
        if cache_hit:
            bulk_cache_hits += 1
        else:
            bulk_provider_fetches += 1
            last_provider_fetch_finished = time.monotonic()
        invalid_identity_rows = int(frame.attrs.get("invalid_ticker_rows", 0))
        if invalid_identity_rows:
            invalid_identity_rows_by_session[
                session.date().isoformat()
            ] = invalid_identity_rows
        if not frame["date"].eq(session).all():
            raise DataFoundationError(
                f"FMP EOD bulk returned rows outside {session.date()}"
            )
        bulk_frames.append(frame)
    mapped = map_eod_bulk_to_security_ids(
        pd.concat(bulk_frames, ignore_index=True),
        security_frames["symbols"],
        security_universe,
    )
    recent_source = normalize_coverage_bars(
        mapped,
        target_session=target,
        ingestion_run_id=run_id,
    )
    history_source = (
        normalize_coverage_bars(
            history_delta,
            target_session=target,
            ingestion_run_id=run_id,
        )
        if not history_delta.empty
        else pd.DataFrame(columns=recent_source.columns)
    )
    mapped_source = pd.concat(
        [history_source, recent_source], ignore_index=True
    )
    duplicate_source = mapped_source.loc[
        mapped_source.duplicated(["date", "security_id"], keep=False)
    ]
    if not duplicate_source.empty:
        numeric = ["open", "high", "low", "close", "adj_close", "volume"]
        conflicts = (
            duplicate_source.groupby(["date", "security_id"])[numeric]
            .nunique(dropna=False)
            .max(axis=1)
            .gt(1)
        )
        if conflicts.any():
            raise DataFoundationError(
                "bulk EOD and identity-delta history conflict for "
                f"{int(conflicts.sum())} date/security keys"
            )
        if "unadjusted_close" in mapped_source.columns:
            mapped_source["unadjusted_close"] = mapped_source.groupby(
                ["date", "security_id"], sort=False
            )["unadjusted_close"].ffill()
        mapped_source = mapped_source.drop_duplicates(
            ["date", "security_id"], keep="last"
        )
    mapped, quarantine = split_coverage_bar_quality(mapped_source)
    quarantine_path = run_dir / "bar_quarantine.parquet"
    quarantine.loc[:, BAR_QUARANTINE_COLUMNS].to_parquet(
        quarantine_path, index=False, compression="snappy"
    )
    quarantine_checks = coverage_bar_quarantine_checks(
        quarantine,
        source_row_count=len(mapped_source),
        security_universe=security_universe,
        target_session=target,
        max_ratio=float(settings.max_bar_quarantine_ratio),
        max_target_ratio=float(settings.max_target_bar_quarantine_ratio),
    )
    if not all(check.passed for check in quarantine_checks):
        detail = "; ".join(
            f"{check.name}: {check.message}"
            for check in quarantine_checks
            if not check.passed
        )
        raise DataFoundationError(f"provider bad-bar quarantine rejected: {detail}")
    mapped = _attach_month_end_nominal_close(mapped, after=parent_target, target=target)
    broad_reader = BroadCoverageReader(market_reader=market_reader)
    previous_overlap = broad_reader.load_bars(start=refresh_start, end=parent_target, version=parent)
    adjustment_audit = _coverage_rebase_audit(
        previous_overlap, mapped, parent_security_ids=parent_presence_ids,
    )
    changed_scales = [row for row in adjustment_audit if any(
        not np.isclose(value, 1., rtol=1e-12, atol=0.) for value in row["scales"].values()
    )]
    affected_months = {
        str(period)
        for period in pd.period_range(refresh_start, target, freq="M")
    }
    affected_months.update(
        mapped["date"].dt.to_period("M").astype(str).unique().tolist()
    )
    if security_master_rebase or changed_scales:
        affected_months.update({
            f"{int(entry['year']):04d}-{int(entry['month']):02d}"
            for entry in json.loads(
                Path(parent.bars_path).read_text(encoding="utf-8")
            )["partitions"]
        })
    unchanged_paths, _replaced_paths, rebuild_months = _parent_partition_paths(
        parent, affected_months=affected_months
    )
    rebuilt_paths: list[Path] = []
    checkpoint_path = run_dir / "checkpoint.json"
    checkpoint = {
        "schema_version": 1,
        "status": "RUNNING",
        "run_id": run_id,
        "target_session": target.date().isoformat(),
        "refresh_start": refresh_start.date().isoformat(),
        "parent_dataset_version_id": parent.version_id,
        "security_master_generation_id": security_generation.generation_id,
        "parent_security_master_generation_id": parent_manifest.get(
            "security_master_generation_id"
        ),
        "security_master_rebase": security_master_rebase,
        "price_scale_reconciliation": adjustment_audit,
        "identity_delta_security_count": len(identity_delta_ids),
        "identity_delta_security_ids": identity_delta_ids,
        "identity_delta_history_rows": len(history_delta),
        "identity_delta_history_cache_hit": history_cache_hit,
        "identity_delta_alias_fallbacks": alias_fallbacks,
        "identity_delta_audit_path": str(identity_delta_audit_path),
        "affected_months": sorted(affected_months),
        "rebuild_months": sorted(rebuild_months),
        "bulk_sessions": len(refresh_sessions),
        "bulk_cache_hits": bulk_cache_hits,
        "bulk_provider_fetches": bulk_provider_fetches,
        "provider_cache_binding": provider_cache_binding,
        "provider_cache_dir": str(provider_cache_dir),
        "mapped_rows": len(mapped),
        "provider_source_rows": len(mapped_source),
        "provider_invalid_identity_rows": sum(
            invalid_identity_rows_by_session.values()
        ),
        "provider_invalid_identity_rows_by_session": (
            invalid_identity_rows_by_session
        ),
        "quarantined_rows": len(quarantine),
        "quarantine_path": str(quarantine_path),
        "quarantine_sha256": _sha256(quarantine_path),
        "periods": {},
    }
    atomic_save_json(checkpoint, checkpoint_path)
    process_lock = Path(args.output_dir).resolve() / ".writer.lock"
    with file_lock(process_lock):
        for period_text in sorted(rebuild_months):
            period = pd.Period(period_text, freq="M")
            period_start = max(
                pd.Timestamp(parent.min_date), period.start_time.normalize()
            )
            period_end = min(target, period.end_time.normalize())
            old_end = min(pd.Timestamp(parent.target_session), period_end)
            old = (
                broad_reader.load_bars(
                    start=period_start,
                    end=old_end,
                    version=parent,
                )
                if period_start <= old_end
                else pd.DataFrame()
            )
            if not old.empty:
                old = old.loc[
                    old["security_id"].astype(str).isin(security_id_set)
                ].copy()
                old = _rebase_coverage_partition(old, adjustment_audit)
            delta = mapped.loc[
                mapped["date"].dt.to_period("M").eq(period)
            ].copy()
            if old.empty and delta.empty:
                continue
            combined = pd.concat([old, delta], ignore_index=True)
            # EOD overlap may not include the independently sourced nominal
            # field. Preserve it from the same historical date/security key.
            if "unadjusted_close" in combined.columns:
                combined["unadjusted_close"] = combined.groupby(
                    ["date", "security_id"], sort=False
                )["unadjusted_close"].ffill()
            combined = (
                combined.drop_duplicates(["date", "security_id"], keep="last")
                .sort_values(["date", "security_id"])
                .reset_index(drop=True)
            )
            path = (
                run_dir
                / "partitions"
                / f"year={int(period.year)}"
                / f"month={int(period.month):02d}.parquet"
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            combined.to_parquet(path, index=False, compression="snappy")
            rebuilt_paths.append(path)
            checkpoint["periods"][period_text] = {
                "status": "SUCCESS",
                "security_count": int(combined["security_id"].nunique()),
                "artifact": {
                    "path": str(path),
                    "sha256": _sha256(path),
                    "rows": len(combined),
                    "year": int(period.year),
                    "month": int(period.month),
                },
            }
            checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
            checkpoint["peak_rss_mb"] = _rss_mb()
            atomic_save_json(checkpoint, checkpoint_path)

    candidate_paths = [*unchanged_paths, *rebuilt_paths]
    presence_ids: set[str] = set()
    connection = catalog._connect(read_only=True)
    try:
        if candidate_paths:
            rows = connection.execute(
                "SELECT DISTINCT security_id FROM read_parquet(?, hive_partitioning=false, union_by_name=true)",
                [[str(path) for path in candidate_paths]],
            ).fetchall()
            presence_ids = {str(row[0]) for row in rows}
    finally:
        connection.close()
    missing_ids = sorted(set(security_ids) - presence_ids)
    presence_check = QualityCheck(
        "selected_security_bar_presence",
        not missing_ids,
        {
            "selected": len(security_ids),
            "observed": len(presence_ids),
            "missing_sample": missing_ids[:20],
        },
        {"missing": 0},
        "every selected coverage security has historical bars",
    )
    identity_delta_check = QualityCheck(
        "security_master_identity_delta_history",
        not alias_failures,
        {
            "security_count": len(identity_delta_ids),
            "history_rows": len(history_delta),
            "alias_failure_count": len(alias_failures),
            "alias_fallback_count": len(alias_fallbacks),
            "audit_path": str(identity_delta_audit_path),
        },
        {"alias_failure_count": 0},
        "new identities have complete history through the parent target and "
        "bulk-EOD coverage afterward",
    )
    store = BroadCoverageStore(
        catalog=catalog,
        lake_dir=CONFIG.abs_path(str(CONFIG.data.foundation.lake_dir)),
    )
    publication = None
    if args.publish:
        publication = store.publish_partitions(
            candidate_paths,
            security_universe=security_universe,
            target_session=target,
            security_master=security_generation,
            price_semantics=build_price_semantics_contract(
                source=(
                    "FMP_FULL_PLUS_DIVIDEND_ADJUSTED_AND_EOD_BULK_"
                    "WITH_ADJUSTED_CLOSE"
                ),
                history_mode="INCREMENTAL_FROM_AUTHENTICATED_PARENT",
            ),
            price_semantics_parent_version_id=parent.version_id,
            min_target_coverage=float(settings.min_target_coverage),
            external_checks=[presence_check, identity_delta_check, *quarantine_checks],
            run_id=run_id,
            bar_quarantine_path=quarantine_path,
            quality_lineage={
                "policy": "PROVIDER_BAD_BAR_QUARANTINE_V1",
                "nominal_price_source": {
                    "field": "unadjusted_close",
                    "endpoint": "FMP/stable/historical-price-eod/non-split-adjusted",
                    "scope": "new_completed_month_ends_and_identity_delta_history",
                },
                "parent_dataset_version_id": parent.version_id,
                "price_scale_reconciliation": {
                    "method": "FRESH_OVERLAP_BY_SECURITY_ID_V1",
                    "parent_manifest_sha256": parent.manifest_checksum_sha256,
                    "overlap_start": refresh_start.date().isoformat(),
                    "overlap_end": parent_target.date().isoformat(),
                    "changed_security_count": len(changed_scales),
                    "audit": adjustment_audit,
                },
                "source_row_count": len(mapped_source),
                "accepted_row_count": len(mapped),
                "quarantined_row_count": len(quarantine),
                "quarantine_sha256": _sha256(quarantine_path),
                "provider_invalid_identity_rows": sum(
                    invalid_identity_rows_by_session.values()
                ),
                "provider_invalid_identity_rows_by_session": (
                    invalid_identity_rows_by_session
                ),
                "identity_delta_audit_path": str(identity_delta_audit_path),
                "identity_delta_audit_sha256": _sha256(identity_delta_audit_path),
            },
        )
        checks = list(publication.checks)
        stats = publication.statistics
    else:
        checks, stats = store._validate_partitions(
            candidate_paths,
            security_universe=security_universe,
            target_session=target,
            min_target_coverage=float(settings.min_target_coverage),
        )
        checks.extend([presence_check, identity_delta_check, *quarantine_checks])
    passed = all(check.passed for check in checks)
    checkpoint.update({
        "status": "PUBLISHED" if publication else "PASS" if passed else "FAIL",
        "quality_checks": [check.to_dict() for check in checks],
        "statistics": stats,
        "unaffected_partition_count": len(unchanged_paths),
        "rebuilt_partition_count": len(rebuilt_paths),
        "rebuilt_months": sorted(rebuild_months),
        "publication": publication.version.to_dict() if publication else None,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mb": round(_rss_mb(), 3),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })
    atomic_save_json(checkpoint, checkpoint_path)
    report_path = run_dir / "audit.json"
    atomic_save_json(checkpoint, report_path)
    return {
        "status": checkpoint["status"],
        "target_session": target.date().isoformat(),
        "refresh_start": refresh_start.date().isoformat(),
        "bulk_sessions": len(refresh_sessions),
        "identity_delta_history_cache_hit": history_cache_hit,
        "bulk_cache_hits": bulk_cache_hits,
        "bulk_provider_fetches": bulk_provider_fetches,
        "provider_cache_binding": provider_cache_binding,
        "provider_cache_dir": str(provider_cache_dir),
        "mapped_rows": len(mapped),
        "provider_source_rows": len(mapped_source),
        "provider_invalid_identity_rows": sum(
            invalid_identity_rows_by_session.values()
        ),
        "provider_invalid_identity_rows_by_session": (
            invalid_identity_rows_by_session
        ),
        "quarantined_rows": len(quarantine),
        "quarantine_path": str(quarantine_path),
        "quarantine_sha256": _sha256(quarantine_path),
        "unaffected_partition_count": len(unchanged_paths),
        "rebuilt_partition_count": len(rebuilt_paths),
        "rebuilt_months": sorted(rebuild_months),
        "publication": checkpoint["publication"],
        "report_path": str(report_path),
        "report_sha256": _sha256(report_path),
        "elapsed_seconds": checkpoint["elapsed_seconds"],
        "peak_rss_mb": checkpoint["peak_rss_mb"],
    }, 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_local_env(args.env_file)
    try:
        result, code = run(args)
    except Exception as exc:  # noqa: BLE001
        if args.json:
            print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"{result['status']} target={result['target_session']} "
            f"rebuilt={result.get('rebuilt_partition_count', 0)} "
            f"reused={result.get('unaffected_partition_count', 0)}"
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
