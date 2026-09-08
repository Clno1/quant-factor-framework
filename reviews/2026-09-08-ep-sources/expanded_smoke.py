#!/usr/bin/env python3
"""Expand known-source coverage on a new DB copy; no profiles, LLM or delivery."""
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
from src.breakouts.ep.fact_review import review_template
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
    original_db = sqlite3.connect(args.archive.resolve().as_uri() + "?mode=ro", uri=True)
    target_db = sqlite3.connect(args.db)
    try:
        original_db.backup(target_db)
    finally:
        original_db.close()
        target_db.close()
    store = EpStore(args.db)
    original = store.report()
    client = PublicArticleClient(max_requests=20, deadline_seconds=180)
    results = []
    for symbol in ("VEEV", "GTLB", "SNOW", "AFRM", "NYAX", "DELL", "SAIC", "PLAB", "ANF"):
        result = SourceEnricher(store, client, max_documents=1).run(original["run_id"], symbol=symbol)
        sources = [row for row in result["sources"] if row["ticker"] == symbol]
        summaries = []
        for row in sources:
            details = {key: row.get(key) for key in ("source_id", "document_id", "status", "final_url", "verification",
                "body_characters", "paragraph_count", "cache_used", "trace")}
            if row.get("content_id"):
                dossier = review_template(store.source_detail(row["source_id"]), max_paragraphs=12)
                details["review_dossier"] = {key: dossier[key] for key in ("status", "blockers", "issuer_attributions",
                    "topic_counts", "matching_paragraphs", "omitted_matching_paragraphs", "total_paragraphs")}
            summaries.append(details)
        results.append({"ticker": symbol, "batch_status": result["status"], "summary": result["summary"], "sources": summaries})
        print(json.dumps({"ticker": symbol, "status": result["status"], "requests_so_far": client.requests}), flush=True)
    assert store.report(original["run_id"]) == original
    report = {"executed_at": datetime.now(timezone.utc).isoformat(), "database": str(args.db.resolve()),
        "archive": str(args.archive.resolve()), "scope": "NINE_KNOWN_TICKERS_NOT_BLIND_SCAN",
        "http_requests": client.requests, "original_evaluations_unchanged": True,
        "profiles_not_fabricated": True, "human_reviews_submitted": 0, "llm_calls": 0, "discord_messages": 0,
        "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
