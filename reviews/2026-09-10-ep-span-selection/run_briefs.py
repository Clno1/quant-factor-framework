"""Render local archived candidate briefs, without collection, LLM calls or delivery."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.breakouts.ep.brief import candidate_brief, render_brief
from src.breakouts.ep.store import EpStore


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--tickers", nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    store = EpStore(args.db, read_only=True)
    as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
    briefs = [candidate_brief(store, symbol, as_of=as_of) for symbol in args.tickers]
    args.output_dir.mkdir()
    with (args.output_dir / "briefs.json").open("x") as output:
        json.dump({"briefs": briefs, "external_requests": 0, "budget_writes": 0}, output, ensure_ascii=False, indent=2)
        output.write("\n")
    with (args.output_dir / "briefs.txt").open("x") as output:
        output.write("\n\n".join(render_brief(brief) for brief in briefs) + "\n")
    print(json.dumps([{"ticker": b["ticker"], "status": b["status"], "sources": len(b["sources"]),
                       "headlines": len(b["headline_hints"]), "omitted": b["coverage"],
                       "freshness": [s["freshness"]["status"] for s in b["sources"]]} for b in briefs]))
