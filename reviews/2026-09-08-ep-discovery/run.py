"""One-shot discovery validation into a durable, git-ignored shadow evidence database."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.breakouts.ep.discovery import OfficialSourceDiscovery, load_registry
from src.breakouts.ep.dossier import candidate_dossier
from src.breakouts.ep.store import EpStore
from src.data.sec_attachments import SecDisclosureClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reuse-db", action="store_true", help="Append a new discovery batch to an existing shadow database")
    args = parser.parse_args()
    if (args.db.exists() and not args.reuse_db) or args.output.exists():
        parser.error("Choose new output paths; previous evidence is never overwritten")
    if args.reuse_db and not args.db.is_file():
        parser.error("Reuse requires an existing shadow database")
    before = EpStore(args.source_db, read_only=True).report()
    contact = os.getenv("SEC_CONTACT_EMAIL")
    client = SecDisclosureClient(contact_email=contact, max_requests=35, deadline_seconds=180, timeout_seconds=15)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    if not args.reuse_db:
        with sqlite3.connect(args.source_db.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(args.db) as target:
                source.backup(target)
    store = EpStore(args.db)
    registry = load_registry(ROOT / "configs/ep_sources.json")
    result = OfficialSourceDiscovery(store, client, registry, max_documents=10).run(before["run_id"])
    assert store.report(before["run_id"]) == before
    old_time = datetime.fromisoformat(before["finished_at"])
    assert store.fetch_history(before["run_id"], as_of=old_time) == []
    dossiers = [candidate_dossier(store, symbol) for symbol in registry["issuers"]]
    output = {"finished_at": datetime.now(timezone.utc).isoformat(), "evidence_db": str(args.db.resolve()),
              "schema_version": store.schema_version, "discovery": result, "dossiers": dossiers,
              "fetches": store.fetch_history(before["run_id"]), "original_collection_unchanged": True,
              "backfill_not_visible_at_original_asof": True, "llm_calls": 0, "discord_messages": 0,
              "deployment_changes": False, "notification_audience": "OWNER_ONLY_USER_DECLARED"}
    encoded = json.dumps(output, ensure_ascii=False, indent=2)
    if contact and contact in encoded:
        raise ValueError("Contact must not appear in audit output")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded + "\n")
    print(json.dumps({"summary": result["summary"], "evidence_db": str(args.db),
                      "dossiers": [{"ticker": d["ticker"], "status": d["status"], "source_count": len(d["sources"])}
                                   for d in dossiers]}), flush=True)


if __name__ == "__main__":
    main()
