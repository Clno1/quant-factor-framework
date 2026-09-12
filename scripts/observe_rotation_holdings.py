#!/usr/bin/env python3
"""Refresh current ETF holdings observations. Does not publish rotation or send.

Writes independent files under
``data/reference/group_analytics/rotation/holdings/<ETF>/<captured_at>.json``.
Member prices go into the group-owned canonical cache (refresh optional).
Per-ETF failures are recorded and do not abort the remaining funds.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.alerts.config import load_local_env
from src.group_analytics.calendar import _calendar, latest_completed_session
from src.group_analytics.rotation.holdings import (
    default_holdings_root,
    normalize_observation,
    observation_breadth,
    save_observation,
)
from src.group_analytics.rotation.service import load_frames
from src.group_analytics.rotation.store import encoded
from src.group_analytics.rotation.themes import default_themes, proxy_etf_symbols


def _symbols(etf, all_proxies):
    if all_proxies:
        return list(proxy_etf_symbols())
    return [etf]


def _observe_one(etf, *, holdings_root, cache_root, sessions, end, max_members, refresh_members):
    from src.data.fmp import _get

    rows = _get("/etf/holdings", {"symbol": etf})
    observation = normalize_observation(rows, etf, pd.Timestamp.now(tz="UTC"))
    if len(observation["members"]) > max_members:
        raise ValueError("Member limit exceeded; increase --max-members explicitly")
    path = save_observation(holdings_root, observation)
    members = [m["ticker"] for m in observation["members"]]
    frames = {}
    errors = []
    if refresh_members and members:
        frames = load_frames(
            members, sessions[0].date().isoformat(), end.date().isoformat(),
            refresh=True, cache_root=cache_root,
        )
        errors = [{"symbol": symbol, "error_type": "MissingCanonicalPrice"}
                  for symbol in members if symbol not in frames]
    elif members:
        frames = load_frames(
            members, sessions[0].date().isoformat(), end.date().isoformat(),
            refresh=False, cache_root=cache_root,
        )
    measured = None
    try:
        measured = observation_breadth(observation, frames, sessions)
        measured["download_errors"] = errors
        measured["observation_sha256"] = observation["response_sha256"]
        measured["observation_path"] = str(path)
    except ValueError as exc:
        measured = {"error_type": type(exc).__name__, "observation_path": str(path),
                    "download_errors": errors}
    return {"etf": etf, "status": "SUCCESS", "observation_path": str(path),
            "members": len(observation["members"]), "excluded": len(observation["excluded"]),
            "reported_weight_pct": observation["reported_weight_pct"],
            "measurement": measured}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--etf", default="SMH", choices=sorted(proxy_etf_symbols(default_themes())))
    parser.add_argument("--all", action="store_true", help="Refresh all 17 registered ETF proxies")
    parser.add_argument("--holdings-root", type=Path, default=None)
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, help="Optional extra dump directory; must be empty/new")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--max-members", type=int, default=120)
    parser.add_argument("--refresh-members", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args(argv)
    if args.env_file and load_local_env(args.env_file) is None:
        raise ValueError("Missing env file")
    from src.group_analytics.settings import load_group_analytics_settings
    if not load_group_analytics_settings().enabled:
        raise ValueError("Group analytics is disabled")
    holdings_root = args.holdings_root or default_holdings_root()
    if args.output is not None:
        if args.output.exists():
            raise ValueError("Use a new, empty observation output directory")
        args.output.mkdir(parents=True)
    end = latest_completed_session()
    sessions = pd.DatetimeIndex(
        _calendar().sessions_in_range((end - pd.Timedelta(days=70)).date().isoformat(),
                                      end.date().isoformat())
    ).tz_localize(None)
    results = []
    for etf in _symbols(args.etf, args.all):
        try:
            results.append(_observe_one(
                etf, holdings_root=holdings_root, cache_root=args.cache_root,
                sessions=sessions, end=end, max_members=args.max_members,
                refresh_members=args.refresh_members,
            ))
        except Exception as exc:
            results.append({"etf": etf, "status": "FAILED", "error_type": type(exc).__name__})
    succeeded = sum(item["status"] == "SUCCESS" for item in results)
    summary = {
        "status": "SUCCESS" if succeeded else "FAILED",
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "holdings_root": str(holdings_root),
        "results": results,
    }
    if args.output is not None:
        from src.group_analytics.adapters import _atomic_json
        _atomic_json(args.output / "report.json", summary)
    print(encoded(summary).decode())
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
