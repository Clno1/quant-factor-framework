"""Re-query FMP gaps into an isolated audit without changing shadow evidence.

The report describes what the provider returns NOW. It cannot establish what was
available during a past live cycle or convert a failed shadow session into PASS.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.breakouts.live.cup_handle import CUP_HANDLE_ALGORITHM_VERSION  # noqa: E402
from src.data.fmp import get_intraday_ohlcv  # noqa: E402
from src.utils.env import load_local_env  # noqa: E402


def _local_timestamp(value):
    timestamp = pd.Timestamp(value)
    return timestamp.tz_convert("America/New_York").tz_localize(None) if timestamp.tzinfo else timestamp


def _bucket_evidence(frames: dict, gap: dict) -> dict:
    """Compare later responses, without treating native 5min as a replacement feed."""
    start, end = [_local_timestamp(gap[key]) for key in ("gap_start", "gap_end")]
    result = {"timestamp_assumption": "START_LABELLED_EXCHANGE_LOCAL",
              "historical_classification_changed": False, "intervals": {}}
    aggregates = {}
    for interval, frame in frames.items():
        if frame is None:
            result["intervals"][interval] = {"available": False}
            continue
        work = frame.copy()
        index = pd.DatetimeIndex(pd.to_datetime(work.index, errors="coerce"))
        if index.tz is not None:
            index = index.tz_convert("America/New_York").tz_localize(None)
        work.index = index
        work = work.loc[(index >= start) & (index < end)].sort_index()
        expected = pd.date_range(start, end, freq="min" if interval == "1min" else "5min", inclusive="left")
        invalid = int(index.isna().sum())
        duplicate = int(work.index.duplicated().sum())
        values = work[["open", "high", "low", "close", "volume"]].apply(pd.to_numeric, errors="coerce")
        finite = values.map(lambda value: pd.notna(value) and math.isfinite(float(value)))
        nonfinite = int((~finite).any(axis=1).sum())
        nonpositive = int(values.volume.le(0).sum())
        invalid_ohlc = int(((values[["open", "high", "low", "close"]] <= 0).any(axis=1)
            | values.high.lt(values[["open", "low", "close"]].max(axis=1))
            | values.low.gt(values[["open", "high", "close"]].min(axis=1))).sum())
        detail = {"available": True, "rows": len(work), "invalid_response_timestamps": invalid,
                  "duplicate_timestamps": duplicate, "nonfinite_rows": nonfinite,
                  "invalid_ohlc_rows": invalid_ohlc, "nonpositive_volume_rows": nonpositive,
                  "missing_slots": [str(t) for t in expected.difference(work.index)],
                  "off_grid_timestamps": [str(t) for t in work.index.difference(expected)]}
        if not work.empty and not (invalid or duplicate or nonfinite or invalid_ohlc or detail["off_grid_timestamps"]):
            aggregate = {"open": float(values.open.iloc[0]), "high": float(values.high.max()),
                         "low": float(values.low.min()), "close": float(values.close.iloc[-1]),
                         "volume": float(values.volume.sum())}
            detail["observed_ohlcv"] = aggregate
            aggregates[interval] = aggregate
        result["intervals"][interval] = detail
    if len(aggregates) == 2:
        result["differences"] = {key: {"one_minute_aggregate": aggregates["1min"][key],
                                      "native_five_minute": aggregates["5min"][key]}
            for key in aggregates["1min"]
            if not math.isclose(aggregates["1min"][key], aggregates["5min"][key], rel_tol=1e-9, abs_tol=1e-9)}
        result["comparison"] = "OHLCV_DISAGREEMENT" if result["differences"] else "OBSERVED_VALUES_AGREE"
    else:
        result["comparison"] = "NO_COMPARABLE_PAIR"
    result["can_confirm_no_trade"] = False
    result["can_confirm_provider_gap"] = False
    return result


def audit_provider_gaps(gaps: list[dict], fetch=get_intraday_ohlcv) -> dict:
    responses = []
    results = []
    for ticker, session in sorted({(gap["ticker"], gap["session_date"]) for gap in gaps}):
        selected = [gap for gap in gaps if gap["ticker"] == ticker and gap["session_date"] == session]
        frames = {}
        by_interval = {}
        for interval in ("1min", "5min"):
            request = {"ticker": ticker, "interval": interval, "session_date": session}
            try:
                frame = fetch(ticker, interval=interval, start=session, end=session)
                frames[interval] = frame
                if frame is None or frame.empty:
                    frames[interval] = None
                    responses.append({**request, "status": "EMPTY_RESPONSE"})
                    by_interval[interval] = None
                    continue
                # Store the normalized response separately from all live tables.
                payload = frame.to_json(orient="split", date_format="iso")
                responses.append({
                    **request, "status": "RECEIVED", "row_count": len(frame),
                    "normalized_response_sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    "normalized_response": json.loads(payload),
                })
                counts = {}
                for gap in selected:
                    start, end = _local_timestamp(gap["gap_start"]), _local_timestamp(gap["gap_end"])
                    index = pd.DatetimeIndex(frame.index)
                    if index.tz is not None:
                        index = index.tz_convert("America/New_York").tz_localize(None)
                    counts[gap["gap_start"]] = int(((index >= start) & (index < end)).sum())
                by_interval[interval] = counts
            except Exception as exc:
                # Provider exception text can contain a credential-bearing URL.
                responses.append({**request, "status": "REQUEST_FAILED", "error_type": type(exc).__name__})
                by_interval[interval] = None
                frames[interval] = None
        for gap in selected:
            counts = {
                key: None if value is None else value[gap["gap_start"]]
                for key, value in by_interval.items()
            }
            status = (
                "INCONCLUSIVE_REQUEST" if any(value is None for value in counts.values())
                else "STILL_ABSENT_BOTH_INTERVALS" if not any(counts.values())
                else "ROWS_PRESENT_ON_REQUERY"
            )
            results.append({**gap, "requery_status": status, "row_counts": counts,
                            "bucket_evidence": _bucket_evidence(frames, gap)})
    return {
        "audit_version": "cup-gap-requery-v2",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "historical_observation_unchanged": True,
        "counts_for_shadow_promotion": False,
        "results": results, "responses": responses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=date.fromisoformat)
    parser.add_argument("--algorithm-version", default=CUP_HANDLE_ALGORITHM_VERSION)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--state-path", type=Path, default=PROJECT_ROOT / "outputs/intraday_momentum_monitor/state.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "outputs/data_audits/cup_handle_gaps")
    args = parser.parse_args()
    if args.env_file is not None and load_local_env(args.env_file) is None:
        parser.error("env file does not exist")
    with sqlite3.connect(args.state_path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        gaps = [dict(row) for row in con.execute(
            "SELECT session_date,ticker,algorithm_version,gap_start,gap_end,classification "
            "FROM cup_handle_data_gaps WHERE session_date=? AND algorithm_version=? "
            "ORDER BY ticker,gap_start", (args.session.isoformat(), args.algorithm_version),
        )]
    if len({gap["ticker"] for gap in gaps}) > 20:
        parser.error("more than 20 symbols: audit requires a bounded investigation")
    report = audit_provider_gaps(gaps)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"{args.session}_{uuid4().hex}.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({"report": str(path), "gap_count": len(gaps), "results": report["results"]}))
    return 2 if any(row["requery_status"] == "INCONCLUSIVE_REQUEST" for row in report["results"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
