#!/usr/bin/env python
"""Validate published factor inputs and refresh custom-watchlist market data."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Require factor research from the latest DuckDB data version, then "
            "refresh only custom-watchlist OHLCV."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _parse_args(argv)
    from scripts.run_paper import _refresh_watchlist_ohlcv
    from src.data.foundation import MarketDataReader
    from src.factors.publication import validate_factor_research_publication
    from src.papertrading.definition import STATUS_ACTIVE
    from src.papertrading.store import list_accounts, load_account
    from src.utils.logger import get_logger

    log = get_logger("prepare_paper_data")
    summaries = [
        account
        for account in list_accounts()
        if account.get("status") == STATUS_ACTIVE
    ]
    if not summaries:
        log.info("No active paper accounts; no paper inputs to refresh.")
        return 0

    universes: set[str] = set()
    for summary in summaries:
        account = load_account(str(summary.get("id") or "")) or {}
        universe = str(account.get("universe") or "").strip()
        if universe and not universe.lower().startswith("watchlist:"):
            universes.add(universe.upper())

    reader = MarketDataReader()
    for universe in sorted(universes):
        version = reader.require_latest(universe)
        publication = validate_factor_research_publication(
            universe,
            version=version,
        )
        log.info(
            "[%s] paper preflight accepted research publication %s "
            "for data version %s target=%s",
            universe,
            publication.get("publication_id"),
            version.version_id,
            version.target_session,
        )

    _refresh_watchlist_ohlcv(summaries)
    log.info(
        "Paper inputs ready: named_universes=%s active_accounts=%d",
        sorted(universes),
        len(summaries),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
