#!/usr/bin/env python3
"""Reparse archived responses offline and import matched IR bodies into a new audit DB."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from probe import inspect_html, transcript_period
from src.breakouts.ep.models import timestamp
from src.breakouts.ep.source_verifier import PARSER_VERSION, parse_article, verify_document
from src.breakouts.ep.store import EpStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ep-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overrides", type=Path, required=True)
    args = parser.parse_args()
    if any(path.exists() for path in (args.output_db, args.output, args.overrides)):
        parser.error("Choose new output paths; original audit artifacts must not be overwritten")
    original = json.loads(args.report.read_text())
    with sqlite3.connect(f"{Path(original['database']).as_uri()}?mode=ro", uri=True) as db:
        raw_rows = {name: (received, sha, raw) for name, received, sha, raw in db.execute("SELECT * FROM audit_raw")}
    for name, (_, sha, raw) in raw_rows.items():
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError(f"Archive hash mismatch: {name}")
    before = EpStore(args.ep_db, read_only=True).report()
    with sqlite3.connect(f"{args.ep_db.resolve().as_uri()}?mode=ro", uri=True) as source:
        with sqlite3.connect(args.output_db) as destination:
            source.backup(destination)
    store = EpStore(args.output_db)
    now = datetime.now(timezone.utc)
    batch = store.start_source_run(before["run_id"], {
        "version": "ep-ir-offline-audit-v1", "mode": "SHADOW", "parser_version": PARSER_VERSION,
        "scope": "MATCHED_KNOWN_CASE_IR_ONLY", "origin_report": str(args.report.resolve()),
        "source_route": "EXPLICIT_OVERRIDE", "ingestion_mode": "ARCHIVED_IR_REPARSE",
        "delivery": "DISABLED_SHADOW_ONLY", "llm": "NOT_CONFIGURED"}, now)
    report = {"processed_at": timestamp(now), "parser_version": PARSER_VERSION, "network_requests": 0,
              "origin_report": str(args.report.resolve()), "ir": [], "fmp": [], "ep_db": str(args.output_db.resolve()),
              "sec": original["sec"], "llm_calls": 0, "discord_messages": 0,
              "historical_availability_verified": False, "production_changes": False}
    overrides = {}
    for entry in original["ir"]:
        item = {key: entry[key] for key in ("ticker", "url", "status", "expected_title", "http_requests")}
        row = raw_rows.get(entry["ticker"] + "_IR")
        if row:
            received, sha, raw = row
            item.update(inspect_html(raw, entry["expected_title"]))
            item.pop("candidate_containers", None)
            item.update(retrieved_at=received, raw_sha256=sha)
            parsed = parse_article(raw)
            candidates = [c for c in before["candidates"] if c["ticker"] == entry["ticker"]]
            matches = [(c, event) for c in candidates for event in c["events"]
                       if event["evidence"].get("title") == entry["expected_title"] and "url" in event["evidence"]]
            if len(matches) != 1 or item["verification"]["status"] != "DOCUMENT_MATCHED":
                item["import_status"] = "NO_UNIQUE_MATCH_NO_IMPORT"
            else:
                candidate, event = matches[0]
                verification = verify_document(event, parsed, candidate["identity"])
                if verification["status"] != "DOCUMENT_MATCHED":
                    raise ValueError("Archived event mismatch")
                result = {k: v for k, v in entry.items() if k in {"final_url", "raw_sha256", "trace", "received_at"}}
                result.update(status=verification["status"], verification=verification,
                              requested_url=entry["url"], original_url=event["evidence"]["url"],
                              source_route="EXPLICIT_OVERRIDE", ingestion_mode="ARCHIVED_IR_REPARSE",
                              retrieved_at=received, parser_version=PARSER_VERSION, text_revision=parsed["text_revision"],
                              body_characters=parsed["characters"], paragraph_count=len(parsed["paragraphs"]),
                              llm="NOT_CONFIGURED", delivery="DISABLED_SHADOW_ONLY", cache_used=False)
                source_id = store.save_source_attempt(batch, candidate["ticker"], event, result, now, raw=raw, parsed=parsed)
                overrides[event["document_id"]] = entry["url"]
                item.update(import_status="IMPORTED_ARCHIVED_IR", source_id=source_id, document_id=event["document_id"])
        report["ir"].append(item)
    summary = {"status": "SOURCE_PASS_COMPLETED" if overrides else "NO_SOURCE_TARGETS",
               "scope": "IMPORTED_MATCHED_IR_ONLY_NOT_FULL_RUN_COVERAGE", "counts": {"DOCUMENT_MATCHED": len(overrides)},
               "http_requests": 0, "ratings_enabled": False, "completeness_claimed": False,
               "delivery": "DISABLED_SHADOW_ONLY", "llm": "NOT_CONFIGURED"}
    store.finish_source_run(batch, summary, now)
    assert store.report(before["run_id"]) == before
    assert EpStore(args.ep_db, read_only=True).report() == before
    for entry in report["ir"]:
        if not entry.get("source_id"):
            continue
        detail = store.source_detail(entry["source_id"])
        assert detail["parsed"]["status"] == "EXTRACTED"
        assert any(s["source_id"] == entry["source_id"] for s in store.explain(entry["ticker"])["source_enrichment"]["sources"])
        assert not store.source_report(before["run_id"], as_of=datetime.fromisoformat(before["finished_at"]))["sources"]
    report["integration"] = {**summary, "collection_unchanged": True, "explain_verified": True,
                             "original_asof_excludes_backfill": True}
    for entry in original["fmp"]:
        item = {k: v for k, v in entry.items() if k != "rows"}
        archive = raw_rows.get(entry["ticker"] + "_" + entry["kind"])
        item["rows"] = []
        if archive:
            for row in json.loads(archive[2]):
                body = row.get("content" if entry["kind"] == "TRANSCRIPT" else "text") or ""
                result = {k: row.get(k) for k in ("symbol", "year", "quarter", "period", "date", "publishedDate", "title")}
                result.update(text_characters=len(body), text_sha256=hashlib.sha256(body.encode()).hexdigest(),
                              expected_identity=row.get("symbol") == entry["ticker"])
                if entry["kind"] == "TRANSCRIPT":
                    result.update(normalized_quarter=transcript_period(row),
                                  expected_period=(str(row.get("year")) == str(entry["params"]["year"]) and
                                                   transcript_period(row) == entry["params"]["quarter"]))
                item["rows"].append(result)
        report["fmp"].append(item)
    report["fmp_period_correction"] = "Original probe read quarter; API returns period=Q2/Q4. Revalidated archived records, no new requests."
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    args.overrides.write_text(json.dumps(overrides, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"imported_ir_documents": len(overrides), "ep_db": report["ep_db"], "network_requests": 0}))


if __name__ == "__main__":
    main()
