"""Inspect this frozen recovery's failures without changing policy or publication."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pandas as pd
from src.config import CONFIG
from src.data.broad_coverage import BroadCoverageReader, normalize_coverage_bars, split_coverage_bar_quality
from src.data.broad_history_repair import file_sha256, inherit_quarantine, query_segments
from src.data.foundation import MarketDataCatalog, MarketDataReader
from src.operations.evidence import safe_text
from src.utils.io import atomic_save_json


SCOPE_SHA = "0b1d42593c8d332a126188db88539bf630bc11e704b4aacc6bbb7644b0b6985a"
STAGING = ROOT / "data/lake/staging/us_equity_coverage_incremental/asof=2026-09-11"
REPORT = STAGING / "run=20260912T114630Z_0da7917a/full_history_repair.json"
CACHE = STAGING / "provider_cache/binding=6151dfe7592b274cb278ad8d79ec77264962a507b73e1890b0c9a81fece1d23e/full_security_repair"
LEDGER_VERSION = "562967c01bb54e2ab39454804cc4ac73"
LEDGER_MANIFEST = "7388933abb12306b88ebe05bf51c2eb9fa15b29e1ac51c3af273e29af1326579"
LEDGER_SHA = "e5ea49cc797694e27cc5c02c21f1e54449cf917a8f1a30cde6cca6dc3b2cba4f"


def main():
    raw_report = REPORT.read_bytes()
    recovery = json.loads(raw_report)
    contract = recovery["contract"]
    assert contract["scope_sha256"] == SCOPE_SHA
    scope = REPORT.parent / "overlap_scope_audit.json"
    assert file_sha256(scope) == SCOPE_SHA
    assert len(recovery["errors"]) <= 64
    catalog = MarketDataCatalog(CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)))
    reader = MarketDataReader(catalog=catalog)
    parent = catalog.get_version(contract["parent_dataset_version_id"], universe="US_EQUITY_COVERAGE")
    assert parent.manifest_checksum_sha256 == contract["parent_manifest_sha256"]
    reader.verify_version(parent, require_price_semantics=True)
    source = catalog.get_version(LEDGER_VERSION, universe="US_EQUITY_COVERAGE")
    assert source.manifest_checksum_sha256 == LEDGER_MANIFEST
    manifest = reader.verify_version(source, require_price_semantics=False)
    ledger_path = CONFIG.abs_path(source.manifest_path).parent / manifest["bar_quarantine_path"]
    assert file_sha256(ledger_path) == LEDGER_SHA
    ledger = pd.read_parquet(ledger_path)
    broad = BroadCoverageReader(market_reader=reader)
    results = []
    for failure in recovery["errors"]:
        sid = failure["security_id"]
        result = {**failure, "publishable": False}
        try:
            records = []
            for path in (CACHE / sid).glob("*/attempt=*/failure.json"):
                record = json.loads(path.read_text())
                if (record["contract"].get("security_id") == sid and all(
                    record["contract"].get(k) == v for k, v in contract.items() if k != "method"
                )):
                    records.append((path, record))
            if len(records) != 1:
                raise ValueError("missing or ambiguous original-policy failure record")
            path, record = records[0]
            aliases = pd.DataFrame(record["contract"]["aliases"])
            for column in ("fetch_start", "fetch_end"):
                aliases[column] = pd.to_datetime(aliases[column])
            expected_queries = [[h, q, str(first.date()), str(last.date())]
                for h, q, first, last in query_segments(
                    aliases, sid, record["contract"]["query_mappings"])]
            downloaded_queries = [[a[k] for k in ("historical_ticker", "query_ticker", "start", "end")]
                for a in record["raw_artifacts"]]
            result.update(raw_query_status="COMPLETE" if downloaded_queries == expected_queries else "PARTIAL",
                expected_queries=expected_queries, downloaded_queries=downloaded_queries,
                missing_parent_dates_scope="VERIFIED_DOWNLOADED_RAW_ONLY")
            pieces, inputs = [], []
            for artifact in record["raw_artifacts"]:
                location = (path.parent.parent / artifact["path"]).resolve()
                assert location.is_relative_to((CACHE / sid).resolve())
                assert file_sha256(location) == artifact["sha256"]
                frame = pd.read_parquet(location).reset_index()
                frame["security_id"], frame["ticker"] = sid, artifact["historical_ticker"]
                pieces.append(frame)
                inputs.append({**artifact, "absolute_path": str(location)})
            if not pieces:
                raise ValueError("original fetch has no complete raw alias; missing-date count is unknown")
            full = normalize_coverage_bars(pd.concat(pieces, ignore_index=True),
                target_session=pd.Timestamp(contract["target_session"]),
                ingestion_run_id="failure-audit", source="FROZEN_FMP")
            clean, bad = split_coverage_bar_quality(full)
            previous = broad.load_bars(security_ids=[sid], version=parent)
            assert not previous.empty and set(previous.security_id) == {sid}
            missing = sorted(set(pd.to_datetime(previous.date)) - set(pd.to_datetime(clean.date)))
            result.update(failure_path=str(path), failure_sha256=file_sha256(path),
                aliases=record["contract"]["aliases"], raw_artifacts=inputs,
                previous_rows=len(previous), clean_rows=len(clean), bad_rows=len(bad),
                missing_parent_dates=[str(d.date()) for d in missing],
                target_present=bool(clean.date.eq(pd.Timestamp(contract["target_session"])).any()),
                bad_records=json.loads(bad.to_json(orient="records", date_format="iso")))
            if not bad.empty:
                try:
                    inherit_quarantine(full, approved=ledger.loc[ledger.security_id.eq(sid)], previous=previous)
                    result.update(quarantine_status="EXACT_PRIOR_QUARANTINE", valid_parent_dates_removed=0)
                except Exception as exc:
                    result.update(quarantine_status="BLOCKED", quarantine_error=safe_text(str(exc), limit=1000))
            result["audit_status"] = "INSPECTED_NOT_PUBLISHED"
        except Exception as exc:
            result.update(audit_status="AUDIT_BLOCKED", audit_error=safe_text(str(exc), limit=1000))
        results.append(result)
    import hashlib
    report = {"observed_at": datetime.now(timezone.utc).isoformat(),
        "recovery_report_sha256_at_read": hashlib.sha256(raw_report).hexdigest(),
        "recovery_completed_at_read": recovery["completed"], "scope_sha256": SCOPE_SHA,
        "quarantine_source": {"version_id": LEDGER_VERSION, "manifest_sha256": LEDGER_MANIFEST, "sha256": LEDGER_SHA},
        "publishable": False, "production_policy_changed": False,
        "counts_for_shadow_promotion": False, "results": results}
    output = ROOT / "outputs/data_audits/coverage_failure_inventory" / (uuid4().hex + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_save_json(report, output)
    print(json.dumps({"report": str(output), "results": [{**{k: row.get(k) for k in
        ("ticker", "audit_status", "audit_error", "raw_query_status", "bad_rows", "quarantine_status", "quarantine_error")},
        "missing_parent_date_count": len(row["missing_parent_dates"]) if "missing_parent_dates" in row else None,
        "missing_parent_dates_first10": row["missing_parent_dates"][:10] if "missing_parent_dates" in row else None}
        for row in results]}))


if __name__ == "__main__":
    main()
