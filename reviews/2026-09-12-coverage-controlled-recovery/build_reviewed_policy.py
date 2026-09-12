"""Build a review artifact from this recovery's authenticated failure inventory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd
import yaml

from src.config import CONFIG
from src.data.broad_coverage import coverage_alias_intervals, select_coverage_securities
from src.data.broad_history_repair import file_sha256, load_repair_rules, query_segments
from src.data.foundation import MarketDataCatalog, MarketDataReader
from src.data.security_master_store import SecurityMasterStore

REVIEW = Path(__file__).resolve().parent
SCOPE = "0b1d42593c8d332a126188db88539bf630bc11e704b4aacc6bbb7644b0b6985a"
REVIEWED_QUARANTINE = {
    "SNYR", "QVCG", "ZDAI", "BSEM", "YALA", "WATR", "IMA", "NUR",
    "CIIT", "JDZG", "MWG", "ZXZZT", "OCAC", "IVDA", "BEP", "SKYA",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--inventory-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert file_sha256(args.inventory) == args.inventory_sha256
    inventory = json.loads(args.inventory.read_text())
    assert inventory["scope_sha256"] == SCOPE
    assert inventory["recovery_completed_at_read"] == 5295
    original = ROOT / "data/lake/staging/us_equity_coverage_incremental/asof=2026-09-11/run=20260912T114630Z_0da7917a/full_history_repair.json"
    assert file_sha256(original) == inventory["recovery_report_sha256_at_read"]
    report = json.loads(original.read_text())
    assert report["complete_scope"] and report["completed"] == report["total"] == 5295
    assert report["status"] in {"FAIL", "VALIDATED"} and "interruption" not in report
    assert {(r["security_id"], r["ticker"]) for r in inventory["results"]} == {
        (r["security_id"], r["ticker"]) for r in report["errors"]}
    base = REVIEW / "full_history_repair_rules.candidate2.yaml"
    assert file_sha256(base) == "ef8769ba348c5f0d2ea77bcf2221470d9410e1cd17b45d0654d0a9e8eb1b29cb"
    policy = yaml.safe_load(base.read_text())
    mappings = json.loads((REVIEW / "additional_query_mappings.json").read_text())
    policy["query_mappings"].extend(mappings)
    source = inventory["quarantine_source"]
    assert source == {
        "version_id": "562967c01bb54e2ab39454804cc4ac73",
        "manifest_sha256": "7388933abb12306b88ebe05bf51c2eb9fa15b29e1ac51c3af273e29af1326579",
        "sha256": "e5ea49cc797694e27cc5c02c21f1e54449cf917a8f1a30cde6cca6dc3b2cba4f",
    }
    keys = {(r["security_id"], r["ticker"], r["date"]) for r in policy["prior_quarantine"]["rows"]}
    reviewed = []
    for result in inventory["results"]:
        if result["ticker"] not in REVIEWED_QUARANTINE:
            continue
        assert result["quarantine_status"] == "EXACT_PRIOR_QUARANTINE"
        assert result["valid_parent_dates_removed"] == 0
        assert result["missing_parent_dates"] == [] and result["raw_query_status"] == "COMPLETE"
        assert result["bad_rows"] == len(result["bad_records"]) > 0
        for artifact in result["raw_artifacts"]:
            assert file_sha256(Path(artifact["absolute_path"])) == artifact["sha256"]
        for row in result["bad_records"]:
            keys.add((row["security_id"], row["ticker"], str(pd.Timestamp(row["date"]).date())))
        reviewed.append({"ticker": result["ticker"], "rows": result["bad_rows"]})
    assert {r["ticker"] for r in reviewed} == REVIEWED_QUARANTINE
    policy["prior_quarantine"] = {
        "version_id": source["version_id"], "manifest_sha256": source["manifest_sha256"],
        "quarantine_sha256": source["sha256"],
        "rows": [dict(zip(("security_id", "ticker", "date"), key)) for key in sorted(keys)],
    }
    output = args.output.resolve()
    assert output.parent == REVIEW and not output.exists()
    with output.open("x") as stream:
        stream.write("# Reviewed exact mappings and prior exclusions. Full-scope publication gates remain mandatory.\n")
        yaml.safe_dump(policy, stream, sort_keys=False, allow_unicode=False)
    catalog = MarketDataCatalog(CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)))
    reader = MarketDataReader(catalog=catalog)
    rules, approved, contract = load_repair_rules(output, catalog=catalog, market_reader=reader)
    store = SecurityMasterStore(CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(CONFIG.data.security_master.snapshot_dir)))
    generation, frames = store.load_published()
    assert generation.generation_id == "5c738854ad504f1c863c47cf15bb4a63"
    cfg = CONFIG.data.broad_coverage
    universe = select_coverage_securities(frames["master"], history_start=str(cfg.history_start),
        target_session=pd.Timestamp("2026-09-11"), allowed_asset_types=list(cfg.allowed_asset_types),
        benchmark_tickers=list(cfg.benchmark_tickers), history_policy=frames.get("history_policy"))
    segments = {}
    for sid in sorted({r["security_id"] for r in rules}):
        aliases = coverage_alias_intervals(universe.loc[universe.security_id.eq(sid)], frames["symbols"],
            history_start=str(cfg.history_start), target_session=pd.Timestamp("2026-09-11"))
        segments[sid] = [[h, q, str(start.date()), str(end.date())]
            for h, q, start, end in query_segments(aliases, sid, rules)]
    certificate = {"observed_at": datetime.now(timezone.utc).isoformat(),
        "status": "POLICY_CONTRACT_VALIDATED", "publishable": False,
        "production_policy_changed": False, "counts_for_shadow_promotion": False,
        "inventory_sha256": args.inventory_sha256, "original_report_sha256": file_sha256(original),
        "security_master_generation_id": generation.generation_id,
        "selected_quarantine_rows": len(approved), "reviewed_quarantine": reviewed,
        "contract": contract, "query_segments": segments}
    with output.with_suffix(".validation.json").open("x") as stream:
        json.dump(certificate, stream, indent=2)
    print(json.dumps({"path": str(output), "sha256": file_sha256(output),
        "mappings": len(rules), "quarantine_rows": len(approved), "status": certificate["status"]}))


if __name__ == "__main__":
    main()
