#!/usr/bin/env python3
"""Build a hash-bound, audit-only source revision plan from a failed frozen run.

No publish option is provided. A verified local correction still requires a
separately reviewed publication path and cannot promote intraday shadow days.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.update_us_equity_coverage import (  # noqa: E402
    _load_or_fetch_eod_bulk_session, _stable_sha256, PROVIDER_CACHE_METHOD,
)
from src.config import CONFIG  # noqa: E402
from src.data.broad_coverage import (  # noqa: E402
    BroadCoverageReader, map_eod_bulk_to_security_ids, normalize_coverage_bars,
    select_coverage_securities, split_coverage_bar_quality,
)
from src.data.broad_history_repair import (  # noqa: E402
    file_sha256, fetch_replacement, load_repair_rules, refresh_canonical_sources,
)
from src.data.coverage_revisions import (  # noqa: E402
    REVISION_METHOD, classify_revisions, certify_local_revision,
)
from src.data.foundation import DataFoundationError, MarketDataCatalog, MarketDataReader  # noqa: E402
from src.data.security_master_store import SecurityMasterStore  # noqa: E402
from src.data.universe_ids import US_EQUITY_COVERAGE  # noqa: E402
from src.operations.evidence import safe_text  # noqa: E402
from src.utils.env import load_local_env  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402


def _no_network(*_):
    raise DataFoundationError("frozen source is missing; audit will not fetch a replacement")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope-audit", type=Path, required=True)
    parser.add_argument("--scope-sha256", required=True)
    parser.add_argument("--verify-ticker", action="append", default=[])
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/data_audits/coverage_revisions"))
    args = parser.parse_args(argv)
    args.verify_ticker = [v.strip().upper() for v in args.verify_ticker]
    if len(args.verify_ticker) > 8 or len(set(args.verify_ticker)) != len(args.verify_ticker):
        parser.error("choose at most eight distinct tickers for a bounded full-history check")
    return args


def load_inputs(scope_path: Path, expected_sha256: str):
    if file_sha256(scope_path) != expected_sha256:
        raise DataFoundationError("revision scope SHA-256 mismatch")
    scope = json.loads(scope_path.read_text())
    identity_path = scope_path.parent / "identity_delta_audit.json"
    identity = json.loads(identity_path.read_text())
    cache = Path(identity["provider_cache_dir"]).resolve()
    staging = CONFIG.abs_path(str(CONFIG.data.foundation.lake_dir)).resolve() / "staging"
    if not cache.is_relative_to(staging):
        raise DataFoundationError("frozen provider cache must be inside the staging lake")
    contract = json.loads((cache / "contract.json").read_text())
    if (contract.get("schema_version") != 1 or contract.get("method") != PROVIDER_CACHE_METHOD
            or _stable_sha256(contract) != scope["provider_cache_binding"]
            or identity["provider_cache_binding"] != scope["provider_cache_binding"]):
        raise DataFoundationError("frozen provider cache binding mismatch")
    for key in ("parent_dataset_version_id", "security_master_generation_id"):
        if identity.get(key) != scope.get(key) or contract.get(key) != scope.get(key):
            raise DataFoundationError(f"frozen revision identity binding mismatch: {key}")
    if contract["security_master_manifest_sha256"] != scope["security_master_manifest_sha256"]:
        raise DataFoundationError("frozen revision master hash mismatch")
    target, start = pd.Timestamp(contract["target_session"]), pd.Timestamp(contract["refresh_start"])
    catalog = MarketDataCatalog(CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)))
    reader = MarketDataReader(catalog=catalog)
    parent = catalog.get_version(scope["parent_dataset_version_id"], universe=US_EQUITY_COVERAGE)
    if parent is None or parent.manifest_checksum_sha256 != scope["parent_manifest_sha256"]:
        raise DataFoundationError("revision parent version/manifest mismatch")
    latest = catalog.latest_version(US_EQUITY_COVERAGE)
    if latest is None or latest.version_id != parent.version_id:
        raise DataFoundationError("revision scope has been superseded by a new published parent")
    manifest = reader.verify_version(parent, require_price_semantics=True)
    settings = CONFIG.data.security_master
    master, frames = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(settings.snapshot_dir)),
    ).load_published()
    if (master.generation_id != scope["security_master_generation_id"]
            or master.manifest_sha256 != scope["security_master_manifest_sha256"]):
        raise DataFoundationError("published master differs from the frozen revision scope")
    coverage = CONFIG.data.broad_coverage
    if str(coverage.history_start) != contract["history_start"]:
        raise DataFoundationError("configured history start differs from frozen source contract")
    universe = select_coverage_securities(
        frames["master"], history_start=str(coverage.history_start), target_session=target,
        allowed_asset_types=list(coverage.allowed_asset_types), benchmark_tickers=list(coverage.benchmark_tickers),
        history_policy=frames.get("history_policy"),
    )
    import exchange_calendars as xcals
    sessions = xcals.get_calendar("XNYS").sessions_in_range(start, target)
    expected_sessions = [pd.Timestamp(d).date().isoformat() for d in sessions]
    if contract["sessions"] != expected_sessions or target.date() <= parent.target_session:
        raise DataFoundationError("frozen revision session range is incomplete or invalid")
    bulk, artifacts = [], []
    for session in contract["sessions"]:
        frame, hit, metadata = _load_or_fetch_eod_bulk_session(
            cache_dir=cache, session=pd.Timestamp(session), fetcher=_no_network,
        )
        if not hit:
            raise DataFoundationError("revision audit unexpectedly fetched source data")
        bulk.append(frame)
        artifacts.append({"session": session, "frame_sha256": metadata["frame_sha256"],
                          "manifest_sha256": file_sha256(cache / "eod" / f"session={session}" / "manifest.json"),
                          "fetched_at": metadata.get("fetched_at")})
    mapped = normalize_coverage_bars(
        map_eod_bulk_to_security_ids(pd.concat(bulk, ignore_index=True), frames["symbols"], universe),
        target_session=target, ingestion_run_id="source-revision-audit",
    )
    mapped, quarantine = split_coverage_bar_quality(mapped)
    broad = BroadCoverageReader(market_reader=reader)
    previous = broad.load_bars(start=start, end=parent.target_session, version=parent)
    canonical_ids = set(manifest.get("quality_lineage", {}).get("canonical_history_security_ids", []))
    canonical_ids &= set(universe.security_id)
    mapped, canonical_proofs = refresh_canonical_sources(
        mapped=mapped, previous_overlap=previous, security_ids=canonical_ids, cache_dir=cache,
        contract=contract, universe=universe, symbols=frames["symbols"], refresh_start=start,
        target=target, fetcher=_no_network, cache_only=True,
    )
    ids = [str(row["security_id"]) for row in scope["failures"]]
    if len(ids) != scope["affected_count"] or len(ids) != len(set(ids)):
        raise DataFoundationError("failed revision scope count or uniqueness mismatch")
    plans = classify_revisions(previous, mapped, parent_target=parent.target_session,
                               window_start=start, security_ids=ids)
    quarantined_ids = set(quarantine.security_id) - canonical_ids
    for plan in plans:
        if plan["security_id"] in quarantined_ids:
            plan["status"] = "BLOCKED"
            plan["reasons"].append("QUARANTINED_FROZEN_SOURCE_BAR")
    binding = {"scope_sha256": expected_sha256, "parent_dataset_version_id": parent.version_id,
               "parent_manifest_sha256": parent.manifest_checksum_sha256,
               "security_master_generation_id": master.generation_id,
               "security_master_manifest_sha256": master.manifest_sha256,
               "provider_cache_binding": scope["provider_cache_binding"],
               "identity_audit_sha256": file_sha256(identity_path),
               "window_start": start.date().isoformat(), "parent_target": parent.target_session.isoformat(),
               "target_session": target.date().isoformat(), "method": REVISION_METHOD,
               "coverage_policy_sha256": _stable_sha256(dict(coverage))}
    return {"scope": scope, "binding": binding, "plans": plans, "artifacts": artifacts,
            "canonical_cache_count": len(canonical_proofs), "mapped": mapped, "broad": broad,
            "parent": parent, "universe": universe, "symbols": frames["symbols"],
            "catalog": catalog, "reader": reader, "target": target}


def run(args):
    if args.env_file:
        load_local_env(args.env_file)
    inputs = load_inputs(args.scope_audit, args.scope_sha256)
    binding, plans = inputs["binding"], inputs["plans"]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    destination = args.output_dir.resolve() / f"target={binding['target_session']}" / f"run={run_id}"
    destination.mkdir(parents=True)
    report_path = destination / "audit.json"
    report = {"schema_version": 1, "method": REVISION_METHOD, "mode": "AUDIT_ONLY", "publishable": False,
              "binding": binding, "source_artifacts": inputs["artifacts"],
              "implementation_sha256": {
                  "audit_coverage_revisions.py": file_sha256(Path(__file__)),
                  "coverage_revisions.py": file_sha256(Path(sys.modules[classify_revisions.__module__].__file__)),
                  "broad_history_repair.py": file_sha256(Path(sys.modules[fetch_replacement.__module__].__file__)),
              },
              "canonical_cache_count": inputs["canonical_cache_count"], "plans": plans,
              "classification_counts": dict(Counter(p["status"] for p in plans)),
              "requested_verification_tickers": args.verify_ticker, "verifications": [],
              "status": "RUNNING", "started_at": datetime.now(timezone.utc).isoformat()}
    atomic_save_json(report, report_path)
    if args.verify_ticker:
        from src.data.fmp import get_coverage_historical_ohlcv
        try:
            mappings, quarantine, rules = load_repair_rules(
                CONFIG.abs_path("configs/full_history_repair_rules.yaml"),
                catalog=inputs["catalog"], market_reader=inputs["reader"],
            )
        except Exception as exc:
            report.update(status="FAILED", error=safe_text(str(exc), limit=3000),
                          completed_at=datetime.now(timezone.utc).isoformat())
            atomic_save_json(report, report_path)
            raise
        for ticker in args.verify_ticker:
            selected = [p for p in plans if p.get("ticker") == ticker]
            evidence = {"ticker": ticker, "status": "BLOCKED", "publishable": False}
            try:
                if len(selected) != 1 or selected[0]["status"] != "LOCAL_REVISION_CANDIDATE":
                    raise DataFoundationError("ticker is not a unique eligible local-revision candidate")
                plan = selected[0]
                sid = plan["security_id"]
                old = inputs["broad"].load_bars(security_ids=[sid], version=inputs["parent"])
                fresh = inputs["mapped"].loc[inputs["mapped"].security_id.eq(sid)]
                path, proof = fetch_replacement(
                    cache_dir=destination / "canonical_evidence", contract=binding, security_id=sid,
                    universe=inputs["universe"], symbols=inputs["symbols"], previous=old, recent=fresh,
                    history_start=str(CONFIG.data.broad_coverage.history_start), target=inputs["target"],
                    fetcher=get_coverage_historical_ohlcv, query_mappings=mappings,
                    approved_quarantine=quarantine, rules_contract=rules,
                )
                evidence.update(certify_local_revision(
                    plan, old, fresh, pd.read_parquet(path),
                    parent_target=binding["parent_target"], window_start=binding["window_start"],
                ))
                evidence["canonical_proof"] = proof
            except Exception as exc:
                evidence["error"] = safe_text(str(exc), limit=3000)
            report["verifications"].append(evidence)
            atomic_save_json(report, report_path)
    report["verification_counts"] = dict(Counter(v["status"] for v in report["verifications"]))
    report["status"] = "AUDITED"
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save_json(report, report_path)
    return {key: report[key] for key in ("status", "mode", "publishable", "binding",
                                        "classification_counts", "verification_counts")} | {
        "report_path": str(report_path), "report_sha256": file_sha256(report_path),
    }


def main(argv=None):
    args = _parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": safe_text(str(exc), limit=3000)}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
