#!/usr/bin/env python
"""Publish daily factor research from already validated DuckDB data versions."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute and publish factor artifacts only when the expected "
            "market-data version is available."
        )
    )
    parser.add_argument(
        "--universe",
        action="append",
        dest="universes",
        help="universe to publish; repeat for several (default: configured list)",
    )
    parser.add_argument(
        "--target-session",
        help="required YYYY-MM-DD market-data session",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rebuild even when this data version already has a valid publication",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="KEY=VALUE file (default: project .env.local)",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _configured_universes() -> list[str]:
    from src.research_universes import research_universe_registry

    return [
        entry.universe_id
        for entry in research_universe_registry().full_research_entries()
    ]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    from src.config import CONFIG
    from src.data.foundation import MarketDataReader
    from src.factors.publication import (
        ResearchPublicationError,
        validate_factor_research_publication,
    )
    from src.utils.env import load_local_env
    from src.utils.logger import get_logger
    from src.utils.market_calendar import latest_publishable_xnys_session

    load_local_env(args.env_file)
    log = get_logger("run_factor_research")
    delay = int(getattr(CONFIG.data.foundation, "close_delay_minutes", 120))
    expected = (
        pd.Timestamp(args.target_session).normalize()
        if args.target_session
        else latest_publishable_xnys_session(delay_minutes=delay)
    )
    universes = [
        str(value).strip().upper()
        for value in (args.universes or _configured_universes())
    ]
    universes = list(dict.fromkeys(universes))
    reader = MarketDataReader()
    results: list[dict] = []
    failures: list[str] = []

    for universe in universes:
        try:
            version = reader.require_latest(universe)
            if pd.Timestamp(version.target_session).normalize() != expected:
                raise RuntimeError(
                    f"[{universe}] latest market-data session is "
                    f"{version.target_session}, expected {expected.date()}"
                )
            if not args.force:
                try:
                    publication = validate_factor_research_publication(
                        universe,
                        version=version,
                    )
                except ResearchPublicationError:
                    pass
                else:
                    results.append(
                        {
                            "universe": universe,
                            "status": "NOOP",
                            "target_session": expected.date().isoformat(),
                            "data_version_id": version.version_id,
                            "research_publication_id": publication.get(
                                "publication_id"
                            ),
                        }
                    )
                    continue

            from scripts.run_mvp import run_pipeline

            research_failures = run_pipeline(
                only_universe=universe,
                dataset_version_ids={universe: version.version_id},
                target_session=expected,
            )
            if research_failures:
                raise RuntimeError(
                    f"[{universe}] factor pipeline failed: {research_failures}"
                )
            publication = validate_factor_research_publication(
                universe,
                version=version,
            )
            results.append(
                {
                    "universe": universe,
                    "status": "PUBLISHED",
                    "target_session": expected.date().isoformat(),
                    "data_version_id": version.version_id,
                    "research_publication_id": publication.get("publication_id"),
                }
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("[%s] factor research publication failed: %s", universe, exc)
            failures.append(universe)
            results.append(
                {
                    "universe": universe,
                    "status": "FAILED",
                    "error": str(exc),
                }
            )

    try:
        from src.research_universes.service import (
            publish_cross_universe_assessments,
        )

        cross = publish_cross_universe_assessments(target_session=expected)
        results.append(
            {
                "universe": "CROSS_UNIVERSE",
                "status": "PUBLISHED",
                "target_session": cross.get("target_session"),
                "data_version_id": cross.get("generation_id"),
                "verdict_counts": cross.get("verdict_counts"),
            }
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Cross-universe publication failed: %s", exc)
        failures.append("CROSS_UNIVERSE")
        results.append(
            {
                "universe": "CROSS_UNIVERSE",
                "status": "FAILED",
                "error": str(exc),
            }
        )

    payload = {"results": results, "failures": sorted(set(failures))}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        for result in results:
            print(
                "{universe}: {status} target={target} data_version={version}".format(
                    universe=result["universe"],
                    status=result["status"],
                    target=result.get("target_session") or "-",
                    version=result.get("data_version_id") or "-",
                )
            )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
