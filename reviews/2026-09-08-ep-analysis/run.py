"""Offline acceptance against archived sources; never requests data or sends alerts."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.analysis import analyze_candidate  # noqa: E402
from src.breakouts.ep.models import timestamp  # noqa: E402
from src.breakouts.ep.store import EpStore  # noqa: E402


def metadata_only(value):
    if isinstance(value, list):
        return [metadata_only(item) for item in value]
    if isinstance(value, dict):
        return {key: metadata_only(item) for key, item in value.items() if key != "quote"}
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Refusing to overwrite an existing acceptance report")
    store = EpStore(args.db, read_only=True)
    before = store.report()
    reviews_before = store.review_history(run_id=before["run_id"])
    backup = None
    if args.persist:
        backup = args.db.with_name(args.db.stem + ".before_analysis.sqlite3")
        if backup.exists():
            raise ValueError("Refusing to overwrite the pre-analysis database backup")
        with store.connection() as connection, sqlite3.connect(backup) as destination:
            connection.backup(destination)
        store = EpStore(args.db)
    now = datetime.now(timezone.utc)
    results = {}
    timings = {}
    for symbol in ("GTLB", "ANF", "NYAX", "PLAB", "AFRM"):
        started = perf_counter()
        results[symbol] = (store.analyze_and_save(symbol, now, as_of=now) if args.persist
                           else analyze_candidate(store, symbol, as_of=now))
        timings[symbol] = round(perf_counter() - started, 4)
        for entry in results[symbol]["sources"]:
            source = store.source_detail(entry["source_id"], as_of=now)
            paragraphs = {p["id"]: p["text"] for p in source["parsed"]["paragraphs"]}
            assert source["parsed"]["text_revision"] == entry["text_revision"]
            for row in entry["financials"]["proposals"]:
                assert row["value_text"] in row["evidence"][0]["quote"]
                assert all(e["quote"] == paragraphs[e["paragraph_id"]] for e in row["evidence"])
        assert results[symbol]["eligible_for_rating"] is False
        assert results[symbol]["delivery"] == "DISABLED_SHADOW_ONLY"
    def facts(symbol):
        return [row for src in results[symbol]["sources"] for row in src["financials"]["proposals"]]
    assert any(p["metric"] == "EPS" and p["share_basis"] == "DILUTED" and p["basis"] == "NON_GAAP"
               and p["values"] == ["0.24"] and p["period_text"] == "Q2 FY 2027" for p in facts("GTLB"))
    assert any(p["value_kind"] == "SPECIAL_ITEM_IMPACT" and p["values"] == ["1.75"] for p in facts("ANF"))
    assert any(p["values"] == ["90"] and p["subject_text"] == "IPS" and p["value_kind"] == "COMPANY_ESTIMATE"
               for p in facts("NYAX"))
    assert results["NYAX"]["sources"][0]["catalyst"]["role"] == "ACQUIRER"
    assert any(p["values"] == ["0.50"] and p["basis"] == "NON_GAAP"
               and p["period_text"] == "Third Quarter Fiscal 2026" for p in facts("PLAB"))
    assert any(p["metric"] == "REVENUE" and p["value_kind"] == "COMPANY_GUIDANCE"
               and [Decimal(v) for v in p["normalized_values"]] == [207_000_000, 227_000_000] for p in facts("PLAB"))
    assert results["AFRM"]["sources"] == []
    assert store.report(before["run_id"]) == before
    assert store.review_history(run_id=before["run_id"]) == reviews_before
    report = {"version": "ep-analysis-acceptance-v1", "observed_at": timestamp(now),
        "database": str(args.db.resolve()), "schema": store.schema_version,
        "backup": str(backup.resolve()) if backup else None, "persisted": args.persist,
        "scope": "FIVE_REGISTERED_COMPANIES_ARCHIVED_EVIDENCE_NOT_HISTORICAL_MARKET_REPLAY",
        "http_requests": 0, "llm_requests": 0, "discord_sends": 0,
        "candidate_snapshot_unchanged": True, "human_reviews_unchanged": True,
        "evidence_anchors_verified": True, "elapsed_seconds_by_ticker": timings,
        "quotes": "OMITTED_FROM_REPORT_AVAILABLE_IN_LOCAL_EP_DATABASE",
        "results": metadata_only(results)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"report": str(args.output), "schema": store.schema_version,
        "results": {symbol: value["summary"] for symbol, value in results.items()},
        "elapsed_seconds_by_ticker": timings}, indent=2))


if __name__ == "__main__":
    main()
