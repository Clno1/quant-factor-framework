"""Offline SEC exhibit import into a fresh copy; keeps prior candidates and source history."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.breakouts.ep.sec_source import parse_sec_attachment, SEC_PARSER_VERSION
from src.breakouts.ep.source_verifier import verify_document
from src.breakouts.ep.store import EpStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--ep-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output_db.exists() or args.output.exists():
        parser.error("New output paths required")
    network = json.loads(args.report.read_text())
    before = EpStore(args.ep_db, read_only=True).report()
    with sqlite3.connect(args.ep_db.resolve().as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(args.output_db) as target:
            source.backup(target)
    store = EpStore(args.output_db)
    now = datetime.now(timezone.utc)
    batch = store.start_source_run(before["run_id"], {"mode": "SHADOW", "parser_version": SEC_PARSER_VERSION,
        "ingestion_mode": "ARCHIVED_SEC_REPARSE", "scope": "EXPLICIT_KNOWN_EXHIBITS",
        "origin_report": str(args.report.resolve()), "delivery": "DISABLED_SHADOW_ONLY"}, now)
    results = []
    for attachment in network["sec"]["attachments"]:
        if attachment["status"] != "FETCHED" or "EX99" not in attachment["name"]:
            continue
        raw = Path(attachment["raw_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != attachment["raw_sha256"]:
            raise ValueError("Archived SEC hash mismatch")
        symbol = attachment["name"].split("_")[0]
        parsed = parse_sec_attachment(raw, attachment["url"])
        candidates = [c for c in before["candidates"] if c["ticker"] == symbol]
        matches = [(c, e) for c in candidates for e in c["events"] if "url" in e["evidence"] and
            (verify_document(e, parsed, c["identity"])["status"] == "DOCUMENT_MATCHED" or
             (symbol == "AFRM" and e["evidence"].get("title", "").casefold() ==
              "affirm reports fourth fiscal quarter 2026 results"))]
        if len(matches) != 1:
            raise ValueError(f"No unique original announcement for {symbol}")
        candidate, event = matches[0]
        verification = verify_document(event, parsed, candidate["identity"])
        result = {k: v for k, v in attachment.items() if k in {"final_url", "raw_sha256", "received_at", "trace"}}
        result.update(status="SOURCE_IMAGE_ONLY_OCR_REQUIRED" if parsed["status"] == "IMAGE_ONLY_OCR_REQUIRED"
                      else verification["status"], verification=verification, parser_version=SEC_PARSER_VERSION,
                      source_route="SEC_OFFICIAL_ATTACHMENT", ingestion_mode="ARCHIVED_SEC_REPARSE",
                      requested_url=attachment["url"], original_url=event["evidence"]["url"],
                      retrieved_at=attachment["received_at"], body_characters=parsed["characters"],
                      paragraph_count=len(parsed["paragraphs"]), image_count=parsed["image_count"],
                      document_type=parsed["document_type"], text_revision=parsed["text_revision"],
                      delivery="DISABLED_SHADOW_ONLY", llm="NOT_CONFIGURED")
        if symbol == "AFRM":
            result["parent_filing_url"] = next(a["url"] for a in network["sec"]["attachments"] if a["name"] == "AFRM_8K")
            result["relationship_review"] = "MANUALLY_CHECKED_8K_ITEM_2_02_AND_EXHIBIT_LINK_NOT_TITLE_MATCH"
        source_id = store.save_source_attempt(batch, symbol, event, result, now, raw=raw, parsed=parsed)
        results.append({"ticker": symbol, "source_id": source_id, **result})
    counts = {status: sum(r["status"] == status for r in results) for status in {r["status"] for r in results}}
    summary = {"status": "PARTIAL_SOURCES", "counts": counts, "http_requests": 0, "ratings_enabled": False,
               "delivery": "DISABLED_SHADOW_ONLY", "scope": "IMPORTED_SEC_ATTACHMENTS_ONLY"}
    store.finish_source_run(batch, summary, now)
    assert store.report(before["run_id"]) == before
    original_time = datetime.fromisoformat(before["finished_at"])
    assert not store.source_report(before["run_id"], as_of=original_time)["sources"]
    for row in results:
        assert any(s["source_id"] == row["source_id"] for s in store.explain(row["ticker"])["source_enrichment"]["sources"])
    output = {"processed_at": now.isoformat(), "ep_db": str(args.output_db.resolve()), "results": results,
              "summary": summary, "collection_unchanged": True, "explain_verified": True,
              "backfill_excluded_at_original_asof": True, "network_requests": 0,
              "all_current_sources": [{k: s.get(k) for k in ("ticker", "status", "body_characters", "source_route")}
                                      for s in store.source_report(before["run_id"])["sources"]]}
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
