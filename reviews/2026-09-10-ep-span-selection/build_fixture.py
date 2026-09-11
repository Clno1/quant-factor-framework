"""Build a small, provenance-labelled offline fixture from the read-only export."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.breakouts.ep.models import digest
from reference_cases import reference_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = json.loads(args.archive.read_text())
    needed = {ticker: set() for ticker in ("GTLB", "ANF", "PLAB", "NYAX")}
    needed["NYAX"] = {"p0005", "p0015"}
    for case in reference_cases():
        selection = case["selection"]
        refs = [v for k, v in selection.items() if k.endswith("_span") and v] + selection["period_spans"]
        needed[case["ticker"]].update(v["paragraph_id"] for v in refs)
    records = []
    for record in original["records"]:
        source = record["prepared"]
        prepared = {k: deepcopy(source[k]) for k in ("version", "batch", "document_id", "text_revision", "ticker")}
        prepared["untrusted_paragraphs"] = [p for p in source["untrusted_paragraphs"] if p["id"] in needed[source["ticker"]]]
        prepared["coverage"] = {"included": len(prepared["untrusted_paragraphs"]), "total": source["coverage"]["total"],
            "included_chars": sum(len(p["text"]) for p in prepared["untrusted_paragraphs"]), "complete": False}
        prepared["request_id"] = digest(prepared)
        records.append({"request_key": record["request_key"], "prepared": prepared,
                        "parent_request_id": source["request_id"], "subset_fixture": True,
                        "original_rejection_reasons": [r["reasons"] for r in record["result"]["validation"]["rejected"]]})
    fixture = {"version": "ep-span-reference-fixture-v1", "records": records, "budget": original["budget"],
               "source": "SG archived ep-llm-batches-v3 requests; explicit paragraph subsets, not complete releases",
               "reference_type": "HUMAN_SELECTED_IDS", "external_requests": 0}
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(fixture, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"documents": len(records), "paragraphs": sum(len(r["prepared"]["untrusted_paragraphs"]) for r in records)}))


if __name__ == "__main__":
    main()
