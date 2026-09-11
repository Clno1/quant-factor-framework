"""Offline real-response relationship audit. No keys, requests, or ledger writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.breakouts.ep.llm_associations import audit_associations


def run(fixture):
    results = [{"ticker": row["ticker"], "request_key": row["request_key"],
                **audit_associations(row["packet"], row["response"])} for row in fixture["records"]]
    items = [item for result in results for item in result["items"]]
    return {"results": results, "blocked": sum(i["status"] == "BLOCKED" for i in items),
            "review_required": sum(i["status"] == "REVIEW_REQUIRED" for i in items),
            "financial_semantics_verified": False, "external_requests": 0,
            "budget_writes": 0, "delivery": "DISABLED_SHADOW_ONLY"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(json.loads(args.fixture.read_text()))
    with args.output.open("x") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}))
