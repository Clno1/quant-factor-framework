#!/usr/bin/env python3
"""Audit FMP capabilities required by the US broad-equity data contract.

This command is deliberately read-only with respect to the market-data lake.
It writes a small, redacted JSON report plus a SHA-256 sidecar and never moves
DuckDB publication pointers or stores provider payloads.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import sys
import time
from typing import Any, Callable

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.fmp import (  # noqa: E402
    get_api_key,
    get_delisted_companies,
    get_eod_bulk,
    get_historical_ohlcv,
    get_ipo_calendar,
    get_security_profile,
    get_stock_list,
    get_symbol_changes,
    get_us_active_equities,
)
from src.utils.env import load_local_env  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402
from src.utils.market_calendar import (  # noqa: E402
    latest_publishable_xnys_session,
)


SCHEMA_VERSION = 1
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "data_audits"
MAIN_EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit FMP capabilities for US_EQUITY_COVERAGE",
    )
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--target-session", default=None)
    parser.add_argument("--history-start", default="2019-01-01")
    parser.add_argument("--delisted-pages", type=int, default=3)
    parser.add_argument("--delisted-page-size", type=int, default=100)
    parser.add_argument("--delisted-eod-samples", type=int, default=3)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _rss_mb() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw / (1024.0 * 1024.0) if sys.platform == "darwin" else raw / 1024.0


def _frame_sha256(frame: pd.DataFrame) -> str:
    if frame.empty:
        return hashlib.sha256(b"").hexdigest()
    normalized = frame.copy()
    normalized.columns = normalized.columns.astype(str)
    normalized = normalized.reindex(sorted(normalized.columns), axis=1)
    payload = pd.util.hash_pandas_object(
        normalized.astype(str),
        index=True,
    ).to_numpy().tobytes()
    return hashlib.sha256(payload).hexdigest()


def _frame_summary(frame: pd.DataFrame) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "rows": int(len(frame)),
        "columns": sorted(str(column) for column in frame.columns),
        "frame_sha256": _frame_sha256(frame),
    }
    ticker_column = "ticker" if "ticker" in frame.columns else None
    if ticker_column:
        summary["unique_tickers"] = int(frame[ticker_column].nunique(dropna=True))
    for date_column in ("date", "ipo_date", "delisted_date"):
        if date_column not in frame.columns:
            continue
        values = pd.to_datetime(frame[date_column], errors="coerce").dropna()
        if values.empty:
            continue
        summary[f"{date_column}_min"] = values.min().date().isoformat()
        summary[f"{date_column}_max"] = values.max().date().isoformat()
    return summary


def _redact_error(exc: Exception, secrets: tuple[str, ...]) -> str:
    message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
    return message[:500]


def _run_check(
    name: str,
    operation: Callable[[], pd.DataFrame],
    *,
    secrets: tuple[str, ...],
) -> tuple[dict[str, Any], pd.DataFrame]:
    started = time.perf_counter()
    try:
        frame = operation()
        if not isinstance(frame, pd.DataFrame):
            raise TypeError(f"{name} did not return a DataFrame")
        return {
            "status": "PASS",
            "duration_seconds": round(time.perf_counter() - started, 3),
            **_frame_summary(frame),
        }, frame
    except Exception as exc:  # noqa: BLE001 - audit records provider failures.
        return {
            "status": "FAIL",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(exc).__name__,
            "error": _redact_error(exc, secrets),
        }, pd.DataFrame()


def _load_delisted_pages(page_count: int, page_size: int) -> pd.DataFrame:
    if page_count < 1:
        raise ValueError("delisted-pages must be positive")
    frames: list[pd.DataFrame] = []
    for page in range(page_count):
        frame = get_delisted_companies(page=page, limit=page_size)
        if frame.empty:
            break
        frames.append(frame)
        if len(frame) < page_size:
            break
    if not frames:
        return pd.DataFrame()
    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(["ticker", "delisted_date"], keep="last")
        .sort_values(["delisted_date", "ticker"], ascending=[False, True])
        .reset_index(drop=True)
    )


def _active_eod_samples(target: pd.Timestamp) -> pd.DataFrame:
    start = max(pd.Timestamp("2025-01-01"), target - pd.Timedelta(days=400))
    rows: list[dict[str, Any]] = []
    for ticker in ("AAPL", "MDB", "AEVA"):
        frame = get_historical_ohlcv(
            ticker,
            start.date().isoformat(),
            target.date().isoformat(),
        )
        rows.append({
            "ticker": ticker,
            "available": bool(frame is not None and not frame.empty),
            "rows": int(len(frame)) if frame is not None else 0,
            "date_min": (
                frame.index.min().date().isoformat()
                if frame is not None and not frame.empty else None
            ),
            "date_max": (
                frame.index.max().date().isoformat()
                if frame is not None and not frame.empty else None
            ),
        })
    return pd.DataFrame(rows)


def _delisted_eod_samples(
    delisted: pd.DataFrame,
    *,
    history_start: pd.Timestamp,
    sample_count: int,
) -> pd.DataFrame:
    columns = [
        "ticker", "delisted_date", "available", "rows", "date_min", "date_max",
    ]
    if delisted.empty or sample_count < 1:
        return pd.DataFrame(columns=columns)
    candidates = delisted.copy()
    candidates = candidates.loc[candidates["exchange"].isin(MAIN_EXCHANGES)]
    candidates = candidates.dropna(subset=["delisted_date"])
    candidates = candidates.loc[candidates["delisted_date"].ge(history_start)]
    rows: list[dict[str, Any]] = []
    for row in candidates.head(sample_count).itertuples(index=False):
        delisted_date = pd.Timestamp(row.delisted_date).normalize()
        requested_start = max(history_start, delisted_date - pd.Timedelta(days=400))
        frame = get_historical_ohlcv(
            str(row.ticker),
            requested_start.date().isoformat(),
            delisted_date.date().isoformat(),
        )
        rows.append({
            "ticker": str(row.ticker),
            "delisted_date": delisted_date.date().isoformat(),
            "available": bool(frame is not None and not frame.empty),
            "rows": int(len(frame)) if frame is not None else 0,
            "date_min": (
                frame.index.min().date().isoformat()
                if frame is not None and not frame.empty else None
            ),
            "date_max": (
                frame.index.max().date().isoformat()
                if frame is not None and not frame.empty else None
            ),
        })
    return pd.DataFrame(rows, columns=columns)


def _profile_samples() -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for ticker in ("AAPL", "MDB", "AEVA"):
        profile = get_security_profile(ticker)
        rows.append({
            "ticker": ticker,
            "available": bool(profile),
            "fields": sorted(profile) if profile else [],
            "asset_type": profile.get("asset_type") if profile else None,
            "exchange": profile.get("exchange") if profile else None,
        })
    return pd.DataFrame(rows)


def _decision(checks: dict[str, dict[str, Any]], frames: dict[str, pd.DataFrame]) -> str:
    current_required = (
        "active_equities", "stock_list", "eod_bulk", "active_eod_samples",
    )
    if any(checks[name]["status"] != "PASS" for name in current_required):
        return "ALTERNATE_PROVIDER_OR_CLIENT_FIX_REQUIRED"
    if not bool(frames["active_eod_samples"].get("available", pd.Series(dtype=bool)).all()):
        return "ALTERNATE_PROVIDER_OR_CLIENT_FIX_REQUIRED"

    history_required = ("delisted_companies", "symbol_changes", "ipo_calendar")
    if any(checks[name]["status"] != "PASS" for name in history_required):
        return "PROSPECTIVE_ONLY"
    sampled = frames["delisted_eod_samples"]
    if sampled.empty or not bool(sampled["available"].all()):
        return "PROSPECTIVE_ONLY"
    return "GO_TO_CAPACITY_BENCHMARK"


def run_audit(
    *,
    target_session: pd.Timestamp,
    history_start: pd.Timestamp,
    delisted_pages: int,
    delisted_page_size: int,
    delisted_eod_samples: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    secret = get_api_key()
    secrets = (secret,)
    checks: dict[str, dict[str, Any]] = {}
    frames: dict[str, pd.DataFrame] = {}

    operations: list[tuple[str, Callable[[], pd.DataFrame]]] = [
        ("active_equities", lambda: get_us_active_equities()),
        ("stock_list", get_stock_list),
        (
            "delisted_companies",
            lambda: _load_delisted_pages(delisted_pages, delisted_page_size),
        ),
        ("symbol_changes", get_symbol_changes),
        (
            "ipo_calendar",
            lambda: get_ipo_calendar(
                start=history_start.date().isoformat(),
                end=target_session.date().isoformat(),
            ),
        ),
        ("eod_bulk", lambda: get_eod_bulk(target_session)),
        ("active_eod_samples", lambda: _active_eod_samples(target_session)),
        ("profile_samples", _profile_samples),
    ]
    for name, operation in operations:
        checks[name], frames[name] = _run_check(
            name,
            operation,
            secrets=secrets,
        )

    checks["delisted_eod_samples"], frames["delisted_eod_samples"] = _run_check(
        "delisted_eod_samples",
        lambda: _delisted_eod_samples(
            frames.get("delisted_companies", pd.DataFrame()),
            history_start=history_start,
            sample_count=delisted_eod_samples,
        ),
        secrets=secrets,
    )
    decision = _decision(checks, frames)
    return {
        "schema_version": SCHEMA_VERSION,
        "audit": "FMP_US_EQUITY_COVERAGE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_session": target_session.date().isoformat(),
        "history_start": history_start.date().isoformat(),
        "provider": "FMP",
        "api_key_present": True,
        "api_key_value_recorded": False,
        "parameters": {
            "delisted_pages": int(delisted_pages),
            "delisted_page_size": int(delisted_page_size),
            "delisted_eod_samples": int(delisted_eod_samples),
        },
        "checks": checks,
        "decision": decision,
        "decision_is_final_go": False,
        "next_gate": (
            "Run 100/500/3000-security capacity benchmark on the fixed SG host"
            if decision == "GO_TO_CAPACITY_BENCHMARK"
            else "Resolve failed capabilities before historical backfill"
        ),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mb": round(_rss_mb(), 3),
        "official_contracts": [
            "https://site.financialmodelingprep.com/developer/docs/delisted-companies-api",
            "https://site.financialmodelingprep.com/developer/docs/stable/symbol-changes-list",
            "https://site.financialmodelingprep.com/developer/docs/ipo-calendar-api",
            "https://site.financialmodelingprep.com/developer/docs/stable/eod-bulk",
        ],
    }


def _write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = str(report["target_session"])
    path = output_dir / f"fmp_us_equity_coverage_{target}.json"
    atomic_save_json(report, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    digest_path = path.with_suffix(path.suffix + ".sha256")
    digest_path.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return path, digest_path, digest


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_local_env(args.env_file)
    target = (
        pd.Timestamp(args.target_session).normalize()
        if args.target_session
        else pd.Timestamp(latest_publishable_xnys_session()).normalize()
    )
    history_start = pd.Timestamp(args.history_start).normalize()
    if pd.isna(history_start) or history_start > target:
        raise ValueError("history-start must be on or before target-session")
    report = run_audit(
        target_session=target,
        history_start=history_start,
        delisted_pages=int(args.delisted_pages),
        delisted_page_size=int(args.delisted_page_size),
        delisted_eod_samples=int(args.delisted_eod_samples),
    )
    path, digest_path, digest = _write_report(report, Path(args.output_dir))
    result = {
        "decision": report["decision"],
        "target_session": report["target_session"],
        "report_path": str(path),
        "sha256_path": str(digest_path),
        "sha256": digest,
        "duration_seconds": report["duration_seconds"],
        "peak_rss_mb": report["peak_rss_mb"],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            "decision={decision} target={target_session} report={report_path} "
            "sha256={sha256}".format(**result)
        )
    return 0 if report["decision"] == "GO_TO_CAPACITY_BENCHMARK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
