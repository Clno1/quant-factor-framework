#!/usr/bin/env python3
"""Read-only audit of per-ticker OHLCV files used by the multi-factor pipeline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data.loader import _ohlcv_frame_is_usable  # noqa: E402


def _universe_path(name: str) -> Path:
    return ROOT / "data" / "raw" / "universe" / f"{name.strip().lower()}.parquet"


def audit(universe: str, *, details: int = 25) -> dict[str, object]:
    universe_name = universe.strip().upper()
    path = _universe_path(universe_name)
    if not path.is_file():
        raise FileNotFoundError(f"universe cache does not exist: {path}")
    members = pd.read_parquet(path)
    if "ticker" not in members.columns:
        raise ValueError(f"universe cache has no ticker column: {path}")
    tickers = (
        members["ticker"]
        .dropna()
        .astype(str)
        .str.strip()
        .str.upper()
        .str.replace(".", "-", regex=False)
        .loc[lambda values: values.ne("")]
        .drop_duplicates()
        .tolist()
    )
    raw_root = ROOT / "data" / "raw" / "ohlcv"
    valid: list[str] = []
    missing: list[str] = []
    invalid: list[dict[str, object]] = []
    unreadable: list[dict[str, str]] = []
    for ticker in tickers:
        cache = raw_root / f"{ticker}.parquet"
        if not cache.is_file():
            missing.append(ticker)
            continue
        try:
            frame = pd.read_parquet(cache)
        except Exception as exc:  # noqa: BLE001
            unreadable.append(
                {"ticker": ticker, "error_type": type(exc).__name__}
            )
            continue
        if not _ohlcv_frame_is_usable(frame):
            invalid.append(
                {
                    "ticker": ticker,
                    "columns": list(map(str, frame.columns)),
                    "rows": int(len(frame)),
                }
            )
            continue
        valid.append(ticker)
    problem_count = len(missing) + len(invalid) + len(unreadable)
    return {
        "status": "PASS" if problem_count == 0 else "FAIL",
        "universe": universe_name,
        "universe_count": len(tickers),
        "valid_count": len(valid),
        "invalid_count": len(invalid),
        "missing_count": len(missing),
        "unreadable_count": len(unreadable),
        "invalid_examples": invalid[: max(0, details)],
        "missing_examples": missing[: max(0, details)],
        "unreadable_examples": unreadable[: max(0, details)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default="SP500")
    parser.add_argument("--details", type=int, default=25)
    args = parser.parse_args(argv)
    try:
        result = audit(args.universe, details=args.details)
    except Exception as exc:  # configuration/path only; no secrets in output
        result = {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
