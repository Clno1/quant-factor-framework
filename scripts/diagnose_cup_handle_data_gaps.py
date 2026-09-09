"""Re-query FMP gaps into an isolated audit without changing shadow evidence.

The report describes what the provider returns NOW. It cannot establish what was
available during a past live cycle or convert a failed shadow session into PASS.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
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


def audit_provider_gaps(gaps: list[dict], fetch=get_intraday_ohlcv) -> dict:
    responses = []
    results = []
    for ticker in sorted({gap["ticker"] for gap in gaps}):
        selected = [gap for gap in gaps if gap["ticker"] == ticker]
        session = selected[0]["session_date"]
        by_interval = {}
        for interval in ("1min", "5min"):
            request = {"ticker": ticker, "interval": interval, "session_date": session}
            try:
                frame = fetch(ticker, interval=interval, start=session, end=session)
                if frame is None or frame.empty:
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
                    start, end = pd.Timestamp(gap["gap_start"]), pd.Timestamp(gap["gap_end"])
                    index = pd.DatetimeIndex(frame.index)
                    if index.tz is not None:
                        index = index.tz_convert("America/New_York").tz_localize(None)
                    counts[gap["gap_start"]] = int(((index >= start) & (index < end)).sum())
                by_interval[interval] = counts
            except Exception as exc:
                # Provider exception text can contain a credential-bearing URL.
                responses.append({**request, "status": "REQUEST_FAILED", "error_type": type(exc).__name__})
                by_interval[interval] = None
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
            results.append({**gap, "requery_status": status, "row_counts": counts})
    return {
        "audit_version": "cup-gap-requery-v1",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "historical_observation_unchanged": True,
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
