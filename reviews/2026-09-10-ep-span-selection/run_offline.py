"""Replay human reference IDs against archived announcements. No network or keys."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.llm_span_selection import packet_from_archived_request, validate_selection
from reference_cases import reference_cases, selected_response


def run(archive):
    records = {r["prepared"]["ticker"]: r for r in archive["records"]}
    before = deepcopy(archive)
    results = []
    for case in reference_cases():
        selection = case["selection"]
        contexts = [v for k, v in selection.items() if k.endswith("_span") and v] + selection["period_spans"]
        record = records[case["ticker"]]
        packet = packet_from_archived_request(record["prepared"], paragraph_ids={v["paragraph_id"] for v in contexts})
        response = selected_response(packet, selection)
        checked = validate_selection(packet, response)
        results.append({"ticker": case["ticker"], "original_index": case["original_index"],
                        "parent_request_key": record["request_key"], "reference_type": "HUMAN_SELECTED_IDS",
                        "request_bytes": len(json.dumps(packet["request"], ensure_ascii=False).encode()),
                        "response_bytes": len(json.dumps(response).encode()), "selection_response": response,
                        "validation": checked})
    record = records["NYAX"]
    packet = packet_from_archived_request(record["prepared"], paragraph_ids={"p0005", "p0015"})
    envelope = {k: packet["request"][k] for k in ("request_id", "document_id", "text_revision")}
    control = validate_selection(packet, {**envelope, "scope_status": "UNCERTAIN", "selections": []})
    assert archive == before
    return {"version": "ep-span-offline-review-v1", "external_requests": 0, "budget_snapshot": archive["budget"],
            "reference_cases": len(results), "accepted_reference_cases": sum(bool(r["validation"]["accepted"]) for r in results),
            "unique_materialized_proposals": len({p["proposal_id"] for r in results for p in r["validation"]["accepted"]}),
            "model_selection_accuracy": None, "financial_semantics_verified": False,
            "delivery": "DISABLED_SHADOW_ONLY", "results": results,
            "negative_control": {"ticker": "NYAX", "reference_type": "HUMAN_EMPTY_SELECTION", "validation": control}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(json.loads(args.archive.read_text()))
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k not in {"results", "negative_control"}}))
    return 0 if report["accepted_reference_cases"] == report["reference_cases"] else 2


if __name__ == "__main__":
    sys.exit(main())
