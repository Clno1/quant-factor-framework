"""Build a bounded regression fixture from local, previously exported audit files."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.breakouts.ep.llm_span_selection import packet_from_archived_request, validate_selection


def build(audit, archive):
    records = []
    for row in audit["results"]:
        plan = next(p for p in audit["plans"] if p["request_key"] == row["request_key"])
        prepared = next(r["prepared"] for r in archive["records"]
                        if r["prepared"]["ticker"] == row["ticker"]
                        and r["prepared"]["batch"]["name"] == plan["batch_name"])
        packet = packet_from_archived_request(prepared, paragraph_ids=plan["paragraph_ids"])
        if packet["packet_hash"] != plan["packet_hash"]:
            raise ValueError("ARCHIVE_PACKET_HASH_MISMATCH")
        if validate_selection(packet, row["raw_selection_response"]) != row["replay"]:
            raise ValueError("ARCHIVE_REPLAY_MISMATCH")
        records.append({"ticker": row["ticker"], "request_key": row["request_key"],
                        "packet": packet, "response": row["raw_selection_response"]})
    return {"version": "ep-live-selection-fixture-v1", "origin": "ACTUAL_KIMI_RESPONSES_20260910",
            "scope": "HUMAN_SELECTED_PARAGRAPHS_MODEL_SELECTED_IDS", "records": records,
            "budget_snapshot": audit["budget"], "external_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build(json.loads(args.audit.read_text()), json.loads(args.archive.read_text()))
    with args.output.open("x") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
