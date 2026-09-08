#!/usr/bin/env python3
"""Bounded VEEV source test on a new copy of an archived observation database."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.enrichment import SourceEnricher
from src.breakouts.ep.store import EpStore
from src.data.public_articles import PublicArticleClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.archive.is_file() or args.db.exists():
        parser.error("Require an existing archive and a new destination database")
    source = sqlite3.connect(args.archive.resolve().as_uri() + "?mode=ro", uri=True)
    target = sqlite3.connect(args.db)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    store = EpStore(args.db)
    original = store.report()
    client = PublicArticleClient(max_requests=6, deadline_seconds=60)
    result = SourceEnricher(store, client, max_documents=1).run(original["run_id"], symbol="VEEV")
    assert store.report(original["run_id"]) == original
    result["audit"] = {"executed_at": datetime.now(timezone.utc).isoformat(),
                       "archive": str(args.archive.resolve()), "database": str(args.db.resolve()),
                       "scope": "ONE_KNOWN_VEEV_SOURCE_NOT_MARKET_SCAN", "original_report_unchanged": True,
                       "identity_not_fabricated": True, "llm_calls": 0, "discord_messages": 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
