#!/usr/bin/env python3
"""Exercise v1A against archived, known-list evidence; never access the network."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.models import EpSettings  # noqa: E402
from src.breakouts.ep.service import EpRadar  # noqa: E402
from src.breakouts.ep.store import EpStore  # noqa: E402

ARCHIVE = ROOT / "reviews/2026-09-06-ep-data-capability"


class ArchivedProvider:
    scope = "KNOWN_17_CASES_ARCHIVED_FMP_NOT_MARKET_SCAN"

    def __init__(self):
        cases = json.loads((ARCHIVE / "case_evidence.json").read_text())["cases"]
        self.symbols = {row["ticker"] for row in cases}
        self.news = []
        for symbol in sorted(self.symbols):
            record = json.loads((ARCHIVE / "evidence" / f"case_release_{symbol}.json").read_text())
            for row in record["payload"]:
                if row["symbol"] in self.symbols:
                    self.news.append({**row, "text": row.get("text_excerpt", "")})
        self.news.sort(key=lambda row: row["publishedDate"], reverse=True)

    def articles(self, feed, page, limit, *, timeout):
        return self.news[page * limit:(page + 1) * limit] if feed == "press" else []

    def calendar(self, day, *, timeout):
        path = ARCHIVE / "evidence" / f"calendar_{day}.json"
        if not path.exists():
            raise FileNotFoundError("No archived calendar for this date")
        return [row for row in json.loads(path.read_text())["payload"] if row["symbol"] in self.symbols]

    def profile(self, symbol, *, timeout):
        raise RuntimeError("Profiles intentionally not fabricated in this replay")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.db.exists():
        parser.error("Choose a new database to keep this replay receipt history unambiguous")
    provider = ArchivedProvider()
    radar = EpRadar(EpStore(args.db), provider,
                    EpSettings(max_days=14, max_pages=10, max_profiles=0, max_requests=40))
    result = radar.collect("2026-08-25", "2026-09-03")
    assert {row["ticker"] for row in result["candidates"]} == provider.symbols
    assert all(row["grade"] is None and row["delivery"] == "DISABLED_SHADOW_ONLY" for row in result["candidates"])
    result["audit_notes"] = {
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "fixture_origin": str(ARCHIVE), "database": str(args.db.resolve()),
        "known_list_only": True, "stock_feed_intentionally_empty": True,
        "missing_archive_dates_are_errors_not_zero_events": True,
        "text_is_archived_excerpt_not_fulltext": True,
        "profiles_not_reconstructed": True,
        "published_times_do_not_prove_historical_arrival": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": result["status"], "cases": len(result["candidates"]),
                      "symbols": sorted(provider.symbols), "report": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
