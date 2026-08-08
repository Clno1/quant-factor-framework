#!/usr/bin/env python
"""
Operate the versioned DuckDB/Parquet daily market-data pipeline.

Typical commands:

    python scripts/run_data_pipeline.py pit
    python scripts/run_data_pipeline.py update
    python scripts/run_data_pipeline.py update --universe MAG7 --force
    python scripts/run_data_pipeline.py status

``update`` is the only supported FMP writer.  Research jobs must use
``scripts/run_mvp.py`` and therefore only see catalog versions that passed all
publication checks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest and publish versioned FMP daily market data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pit = subparsers.add_parser(
        "pit",
        help="build and strictly publish the main-factor SP500 PIT universe",
    )
    pit.add_argument(
        "--target-session",
        help="explicit latest XNYS session; default resolves close plus delay",
    )
    pit.add_argument(
        "--start",
        help="fixed reconstruction start; default uses configured main_factor_start",
    )
    pit.add_argument(
        "--candidate-only",
        action="store_true",
        help="write diagnostics without replacing the production membership",
    )
    pit.add_argument(
        "--corrections",
        help="reviewed correction registry path",
    )
    pit.add_argument(
        "--env-file",
        default=None,
        help="KEY=VALUE file (default: project .env.local)",
    )
    pit.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable result JSON",
    )

    update = subparsers.add_parser(
        "update",
        help="download completed XNYS data and publish validated versions",
    )
    update.add_argument(
        "--universe",
        action="append",
        dest="universes",
        help="universe to update; repeat for several (default: configured list)",
    )
    update.add_argument(
        "--target-session",
        help="explicit YYYY-MM-DD session; default resolves close plus delay",
    )
    update.add_argument(
        "--workers",
        type=int,
        default=None,
        help="per-universe FMP ticker concurrency",
    )
    update.add_argument(
        "--force",
        action="store_true",
        help="republish the target session and refresh the overlap window",
    )
    update.add_argument(
        "--run-research",
        action="store_true",
        help="recompute factor artifacts after each successful publication",
    )
    update.add_argument(
        "--env-file",
        default=None,
        help="KEY=VALUE file (default: project .env.local)",
    )
    update.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable result JSON",
    )

    status = subparsers.add_parser(
        "status",
        help="show the currently published version for each universe",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable status JSON",
    )
    return parser.parse_args(argv)


def _configured_universes() -> list[str]:
    from src.config import CONFIG

    values = list(CONFIG.universes.enabled)
    return list(dict.fromkeys(str(value).strip().upper() for value in values))


def _print_update_result(payload: dict, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    version = payload.get("version") or {}
    print(
        "{universe}: {status} target={target} rows={rows} "
        "tickers={tickers} coverage={coverage:.2%} version={version}".format(
            universe=payload["universe"],
            status=payload["status"],
            target=payload["target_session"],
            rows=int(version.get("row_count") or 0),
            tickers=int(version.get("ticker_count") or 0),
            coverage=float(version.get("target_coverage") or 0.0),
            version=version.get("version_id") or "-",
        )
    )


def _run_update(args: argparse.Namespace) -> int:
    from src.data.foundation import MarketDataWriter
    from src.utils.env import load_local_env
    from src.utils.logger import get_logger

    load_local_env(args.env_file)
    log = get_logger("run_data_pipeline")
    universes = [
        str(value).strip().upper()
        for value in (args.universes or _configured_universes())
    ]
    writer = MarketDataWriter()
    failures: list[str] = []
    results: list[dict] = []

    for universe in universes:
        try:
            result = writer.update_universe(
                universe,
                target_session=args.target_session,
                force=bool(args.force),
                workers=args.workers,
            )
            payload = result.to_dict()
            results.append(payload)
            if not args.json:
                _print_update_result(payload, as_json=False)
            if args.run_research:
                from scripts.run_mvp import run_pipeline

                research_failures = run_pipeline(
                    only_universe=universe,
                )
                if research_failures:
                    failures.extend(research_failures)
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] daily data publication failed: %s", universe, exc)
            failures.append(universe)
            results.append(
                {
                    "universe": universe,
                    "status": "FAILED",
                    "error": str(exc),
                }
            )

    if args.json:
        print(
            json.dumps(
                {"results": results, "failures": sorted(set(failures))},
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        )
    return 1 if failures else 0


def _run_pit(args: argparse.Namespace) -> int:
    from src.data.sp500_pit import build_main_sp500_pit
    from src.utils.env import load_local_env

    load_local_env(args.env_file)
    result = build_main_sp500_pit(
        target_session=args.target_session,
        start=args.start,
        candidate_only=bool(args.candidate_only),
        corrections_path=args.corrections,
    )
    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            "SP500 PIT: {status} target={target} start={start} "
            "snapshots={snapshots} inconsistencies={inconsistencies} "
            "membership={membership}".format(
                status=payload["status"],
                target=payload["target_session"],
                start=payload["start"],
                snapshots=payload.get("snapshots") or 0,
                inconsistencies=payload.get("inconsistency_count") or 0,
                membership=payload.get("membership_path") or "-",
            )
        )
        print(f"Diagnostics: {payload['diagnostics_path']}")
    return 0 if result.status in {"PUBLISHED", "CANDIDATE_PASS"} else 2


def _run_status(args: argparse.Namespace) -> int:
    from src.data.foundation import MarketDataCatalog

    versions = MarketDataCatalog().list_latest()
    payload = [version.to_dict() for version in versions]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    elif not versions:
        print("No published market-data versions.")
    else:
        for version in versions:
            print(
                f"{version.universe}: target={version.target_session} "
                f"rows={version.row_count} tickers={version.ticker_count} "
                f"coverage={version.target_coverage:.2%} "
                f"version={version.version_id}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command == "pit":
        return _run_pit(args)
    if args.command == "update":
        return _run_update(args)
    if args.command == "status":
        return _run_status(args)
    raise AssertionError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
