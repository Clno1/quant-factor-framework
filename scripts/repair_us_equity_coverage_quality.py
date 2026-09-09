#!/usr/bin/env python3
"""Derive a publishable coverage candidate from a completed failed backfill.

The source checkpoint and every raw partition remain untouched.  Provider rows
that violate the production OHLCV contract are copied into an authenticated
quarantine ledger; only the exact complement is eligible for publication.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sys
import time
from typing import Any
from uuid import uuid4

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.data.broad_coverage import (  # noqa: E402
    BAR_QUARANTINE_COLUMNS,
    BroadCoverageStore,
    coverage_bar_quarantine_checks,
    split_coverage_bar_quality,
)
from src.data.foundation import (  # noqa: E402
    MarketDataCatalog,
    QualityCheck,
)
from src.data.price_semantics import (  # noqa: E402
    FMP_CANONICAL_SOURCE,
    build_price_semantics_contract,
)
from src.data.security_master_store import SecurityMasterStore  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402


SCHEMA_VERSION = 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run-dir", required=True)
    parser.add_argument(
        "--output-dir",
        default=str(CONFIG.data.broad_coverage.candidate_dir),
    )
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return float(value) / divisor


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _source_artifacts(checkpoint: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    artifacts: list[tuple[str, dict[str, Any]]] = []
    batches = checkpoint.get("batches") or {}
    if not batches:
        raise RuntimeError("source checkpoint has no completed batches")
    for batch_key, batch in sorted(batches.items(), key=lambda item: int(item[0])):
        if batch.get("status") != "SUCCESS":
            raise RuntimeError(f"source batch {batch_key} is not SUCCESS")
        for artifact in batch.get("artifacts") or []:
            artifacts.append((str(batch_key), artifact))
    if not artifacts:
        raise RuntimeError("source checkpoint has no partition artifacts")
    return artifacts


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    source_dir = Path(args.source_run_dir).resolve()
    checkpoint_path = source_dir / "checkpoint.json"
    audit_path = source_dir / "audit.json"
    if not checkpoint_path.is_file() or not audit_path.is_file():
        raise FileNotFoundError("source checkpoint/audit is incomplete")
    source_checkpoint_sha = _sha256(checkpoint_path)
    source_audit_sha = _sha256(audit_path)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if checkpoint.get("status") != "FAIL" or checkpoint.get(
        "current_phase"
    ) != "VALIDATION_FAILED":
        raise RuntimeError(
            "source must be a completed VALIDATION_FAILED backfill"
        )
    if checkpoint.get("alias_failures"):
        raise RuntimeError("provider/alias failures cannot be repaired as bad bars")

    security_settings = CONFIG.data.security_master
    security_store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(security_settings.snapshot_dir)),
    )
    generation, _security_frames = security_store.load_published()
    if checkpoint.get("security_master_generation_id") != generation.generation_id:
        raise RuntimeError("source Security Master generation is no longer published")
    if checkpoint.get("security_master_manifest_sha256") != generation.manifest_sha256:
        raise RuntimeError("source Security Master manifest hash differs")

    universe_path = Path(str(checkpoint["universe_path"])).resolve()
    aliases_path = Path(str(checkpoint["aliases_path"])).resolve()
    if _sha256(universe_path) != checkpoint.get("universe_sha256"):
        raise RuntimeError("source security universe hash mismatch")
    if _sha256(aliases_path) != checkpoint.get("aliases_sha256"):
        raise RuntimeError("source alias interval hash mismatch")
    security_universe = pd.read_parquet(universe_path)

    target = pd.Timestamp(checkpoint["target_session"]).normalize()
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_quality_"
        + uuid4().hex[:8]
    )
    run_dir = (
        Path(args.output_dir).resolve()
        / f"asof={target.date()}"
        / f"run={run_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    _link_or_copy(universe_path, run_dir / "security_universe.parquet")
    _link_or_copy(aliases_path, run_dir / "alias_intervals.parquet")

    accepted_paths: list[Path] = []
    partition_records: list[dict[str, Any]] = []
    quarantines: list[pd.DataFrame] = []
    source_rows = 0
    accepted_rows = 0
    for batch_key, artifact in _source_artifacts(checkpoint):
        source_path = Path(str(artifact["path"])).resolve()
        expected_sha = str(artifact["sha256"])
        if not source_path.is_file() or _sha256(source_path) != expected_sha:
            raise RuntimeError(f"source partition hash mismatch: {source_path}")
        frame = pd.read_parquet(source_path)
        if len(frame) != int(artifact["rows"]):
            raise RuntimeError(f"source partition row count mismatch: {source_path}")
        accepted, quarantine = split_coverage_bar_quality(frame)
        source_rows += len(frame)
        accepted_rows += len(accepted)
        relative = source_path.relative_to(source_dir / "partitions")
        accepted_path = run_dir / "partitions" / relative
        if quarantine.empty:
            _link_or_copy(source_path, accepted_path)
        elif not accepted.empty:
            accepted_path.parent.mkdir(parents=True, exist_ok=True)
            accepted.to_parquet(accepted_path, index=False, compression="snappy")
        if not accepted.empty:
            accepted_paths.append(accepted_path)
        if not quarantine.empty:
            quarantine["source_partition"] = str(
                source_path.relative_to(source_dir)
            )
            quarantine["source_partition_sha256"] = expected_sha
            quarantines.append(quarantine)
        partition_records.append({
            "batch": int(batch_key),
            "year": int(artifact["year"]),
            "source_path": str(source_path),
            "source_sha256": expected_sha,
            "source_rows": len(frame),
            "accepted_path": str(accepted_path) if not accepted.empty else None,
            "accepted_sha256": (
                _sha256(accepted_path) if not accepted.empty else None
            ),
            "accepted_rows": len(accepted),
            "quarantined_rows": len(quarantine),
        })

    quarantine_columns = [
        *BAR_QUARANTINE_COLUMNS,
        "source_partition",
        "source_partition_sha256",
    ]
    quarantine = (
        pd.concat(quarantines, ignore_index=True).loc[:, quarantine_columns]
        if quarantines
        else pd.DataFrame(columns=quarantine_columns)
    )
    quarantine = quarantine.sort_values(
        ["date", "security_id", "quality_reasons"]
    ).reset_index(drop=True)
    quarantine_path = run_dir / "bar_quarantine.parquet"
    quarantine.to_parquet(quarantine_path, index=False, compression="snappy")
    if accepted_rows + len(quarantine) != source_rows:
        raise RuntimeError("accepted and quarantined rows do not reconcile to source")

    observed_ids: set[str] = set()
    for path in accepted_paths:
        observed_ids.update(
            pd.read_parquet(path, columns=["security_id"])["security_id"].astype(str)
        )
    selected_ids = set(security_universe["security_id"].astype(str))
    missing_ids = sorted(selected_ids - observed_ids)
    reconciliation_check = QualityCheck(
        "bar_quarantine_source_reconciliation",
        accepted_rows + len(quarantine) == source_rows,
        {
            "source_rows": source_rows,
            "accepted_rows": accepted_rows,
            "quarantined_rows": len(quarantine),
        },
        "accepted + quarantined = source",
        "every raw provider row has exactly one disposition",
    )
    presence_check = QualityCheck(
        "selected_security_bar_presence_after_quarantine",
        not missing_ids,
        {
            "selected": len(selected_ids),
            "observed": len(observed_ids),
            "missing_sample": missing_ids[:20],
        },
        {"missing": 0},
        "no selected security loses its complete history to quarantine",
    )
    settings = CONFIG.data.broad_coverage
    quarantine_checks = coverage_bar_quarantine_checks(
        quarantine,
        source_row_count=source_rows,
        security_universe=security_universe,
        target_session=target,
        max_ratio=float(settings.max_bar_quarantine_ratio),
        max_target_ratio=float(settings.max_target_bar_quarantine_ratio),
    )
    external_checks = [
        reconciliation_check,
        presence_check,
        *quarantine_checks,
    ]
    catalog = MarketDataCatalog(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path))
    )
    store = BroadCoverageStore(
        catalog=catalog,
        lake_dir=CONFIG.abs_path(str(CONFIG.data.foundation.lake_dir)),
    )
    validation_checks, statistics = store._validate_partitions(
        accepted_paths,
        security_universe=security_universe,
        target_session=target,
        min_target_coverage=float(settings.min_target_coverage),
    )
    passed = all(check.passed for check in [*validation_checks, *external_checks])
    publication = None
    quality_lineage = {
        "policy": "PROVIDER_BAD_BAR_QUARANTINE_V1",
        "source_run_dir": str(source_dir),
        "source_checkpoint_sha256": source_checkpoint_sha,
        "source_audit_sha256": source_audit_sha,
        "source_methodology_version": checkpoint.get("methodology_version"),
        "source_partition_count": len(partition_records),
        "source_row_count": source_rows,
        "accepted_row_count": accepted_rows,
        "quarantined_row_count": len(quarantine),
        "quarantine_sha256": _sha256(quarantine_path),
    }
    if args.publish and passed:
        publication = store.publish_partitions(
            accepted_paths,
            security_universe=security_universe,
            target_session=target,
            security_master=generation,
            price_semantics=build_price_semantics_contract(
                source=FMP_CANONICAL_SOURCE,
                history_mode="FULL_REBUILD",
            ),
            min_target_coverage=float(settings.min_target_coverage),
            external_checks=external_checks,
            run_id=run_id,
            bar_quarantine_path=quarantine_path,
            quality_lineage=quality_lineage,
        )
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "PUBLISHED" if publication else "PASS" if passed else "FAIL",
        "run_id": run_id,
        "target_session": target.date().isoformat(),
        "source_run_dir": str(source_dir),
        "source_checkpoint_sha256": source_checkpoint_sha,
        "source_audit_sha256": source_audit_sha,
        "security_master_generation_id": generation.generation_id,
        "security_master_manifest_sha256": generation.manifest_sha256,
        "source_row_count": source_rows,
        "accepted_row_count": accepted_rows,
        "quarantined_row_count": len(quarantine),
        "quarantine_path": str(quarantine_path),
        "quarantine_sha256": _sha256(quarantine_path),
        "partition_count": len(accepted_paths),
        "partitions": partition_records,
        "quality_checks": [
            check.to_dict() for check in [*validation_checks, *external_checks]
        ],
        "statistics": statistics,
        "quality_lineage": quality_lineage,
        "publication": publication.version.to_dict() if publication else None,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mb": round(_rss_mb(), 3),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    report_path = run_dir / "audit.json"
    atomic_save_json(report, report_path)
    report["report_path"] = str(report_path)
    report["report_sha256"] = _sha256(report_path)
    return report, 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result, exit_code = run(args)
    except Exception as exc:  # noqa: BLE001
        if args.json:
            print(json.dumps({"status": "FAILED", "error": str(exc)}))
        else:
            print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"{result['status']} target={result['target_session']} "
            f"accepted={result['accepted_row_count']} "
            f"quarantined={result['quarantined_row_count']}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
