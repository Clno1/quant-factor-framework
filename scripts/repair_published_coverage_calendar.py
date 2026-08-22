#!/usr/bin/env python3
"""Derive a new coverage version with every non-XNYS row quarantined.

The source publication and its ancestors remain immutable. Only affected month
partitions are rewritten; all inherited quarantine ledgers are authenticated
and carried into the new publication.
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
    DataFoundationError,
    MarketDataCatalog,
    MarketDataReader,
    QualityCheck,
)
from src.data.security_master_store import SecurityMasterStore  # noqa: E402
from src.data.universe_ids import US_EQUITY_COVERAGE  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402


SCHEMA_VERSION = 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-version-id")
    parser.add_argument(
        "--output-dir",
        default=str(CONFIG.data.broad_coverage.candidate_dir),
    )
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _resolve(path: str | Path) -> Path:
    value = Path(path)
    return value.resolve() if value.is_absolute() else (ROOT / value).resolve()


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


def _inherited_quarantine(
    catalog: MarketDataCatalog,
    reader: MarketDataReader,
    source_version_id: str,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    frames: list[pd.DataFrame] = []
    lineage: list[dict[str, Any]] = []
    version_id: str | None = source_version_id
    seen: set[str] = set()
    while version_id:
        if version_id in seen:
            raise DataFoundationError("coverage quality lineage contains a cycle")
        seen.add(version_id)
        version = catalog.get_version(version_id, universe=US_EQUITY_COVERAGE)
        if version is None:
            raise DataFoundationError(
                f"coverage quality parent does not exist: {version_id}"
            )
        manifest = reader.verify_version(
            version, verify_partition_children=False
        )
        quarantine_name = manifest.get("bar_quarantine_path")
        rows = 0
        if quarantine_name:
            path = _resolve(version.manifest_path).parent / str(quarantine_name)
            frame = pd.read_parquet(path)
            missing = sorted(set(BAR_QUARANTINE_COLUMNS) - set(frame.columns))
            if missing:
                raise DataFoundationError(
                    f"inherited quarantine is missing columns: {missing}"
                )
            frame = frame.loc[:, BAR_QUARANTINE_COLUMNS].copy()
            frame["quarantine_origin_version_id"] = version.version_id
            frame["quarantine_origin_sha256"] = str(
                manifest["bar_quarantine_sha256"]
            )
            frames.append(frame)
            rows = len(frame)
        quality_lineage = manifest.get("quality_lineage") or {}
        lineage.append({
            "version_id": version.version_id,
            "target_session": version.target_session.isoformat(),
            "quarantine_rows": rows,
            "quarantine_sha256": manifest.get("bar_quarantine_sha256"),
        })
        version_id = quality_lineage.get("parent_dataset_version_id")
    columns = [
        *BAR_QUARANTINE_COLUMNS,
        "quarantine_origin_version_id",
        "quarantine_origin_sha256",
    ]
    combined = (
        pd.concat(frames, ignore_index=True).loc[:, columns]
        if frames
        else pd.DataFrame(columns=columns)
    )
    return combined, lineage


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    catalog = MarketDataCatalog(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path))
    )
    reader = MarketDataReader(catalog=catalog)
    source = (
        reader.require_version(US_EQUITY_COVERAGE, args.source_version_id)
        if args.source_version_id
        else reader.require_latest(US_EQUITY_COVERAGE)
    )
    source_manifest = reader.verify_version(
        source, verify_partition_children=True
    )

    security_settings = CONFIG.data.security_master
    security_store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(security_settings.snapshot_dir)),
    )
    security_generation, _ = security_store.load_published()
    if source_manifest.get("security_master_generation_id") != security_generation.generation_id:
        raise DataFoundationError(
            "source coverage is not bound to the published Security Master"
        )
    if source_manifest.get("security_master_manifest_sha256") != security_generation.manifest_sha256:
        raise DataFoundationError("source coverage Security Master hash differs")

    security_universe = pd.read_parquet(_resolve(source.universe_path))
    inherited, inherited_lineage = _inherited_quarantine(
        catalog, reader, source.version_id
    )
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_calendar_quality_"
        + uuid4().hex[:8]
    )
    run_dir = (
        Path(args.output_dir).resolve()
        / f"asof={source.target_session.isoformat()}"
        / f"run={run_id}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    _link_or_copy(
        _resolve(source.universe_path), run_dir / "security_universe.parquet"
    )

    accepted_paths: list[Path] = []
    new_quarantines: list[pd.DataFrame] = []
    partitions: list[dict[str, Any]] = []
    accepted_rows = 0
    source_rows = 0
    for source_path in reader.partition_paths(source):
        source_path = Path(source_path).resolve()
        frame = pd.read_parquet(source_path)
        accepted, quarantine = split_coverage_bar_quality(frame)
        source_rows += len(frame)
        accepted_rows += len(accepted)
        if quarantine.empty:
            accepted_path = source_path
        else:
            relative = source_path.relative_to(_resolve(source.manifest_path).parent)
            accepted_path = run_dir / relative
            accepted_path.parent.mkdir(parents=True, exist_ok=True)
            accepted.to_parquet(
                accepted_path, index=False, compression="snappy"
            )
            quarantine["quarantine_origin_version_id"] = source.version_id
            quarantine["quarantine_origin_sha256"] = _sha256(source_path)
            new_quarantines.append(quarantine)
        accepted_paths.append(accepted_path)
        partitions.append({
            "source_path": str(source_path),
            "source_sha256": _sha256(source_path),
            "source_rows": len(frame),
            "accepted_path": str(accepted_path),
            "accepted_sha256": _sha256(accepted_path),
            "accepted_rows": len(accepted),
            "quarantined_rows": len(quarantine),
        })

    new_quarantine = (
        pd.concat(new_quarantines, ignore_index=True)
        if new_quarantines
        else pd.DataFrame(columns=inherited.columns)
    )
    if new_quarantine.empty:
        raise DataFoundationError(
            "source publication contains no newly quarantinable rows"
        )
    if not new_quarantine["quality_reasons"].astype(str).str.contains(
        "NON_XNYS_SESSION", regex=False
    ).all():
        raise DataFoundationError(
            "calendar repair found an unexpected non-calendar quality defect"
        )
    cumulative = pd.concat(
        [inherited, new_quarantine], ignore_index=True
    ).sort_values(["date", "security_id", "quality_reasons"]).reset_index(drop=True)
    quarantine_path = run_dir / "bar_quarantine.parquet"
    cumulative.to_parquet(quarantine_path, index=False, compression="snappy")

    settings = CONFIG.data.broad_coverage
    parent_reconciled = accepted_rows + len(new_quarantine) == source_rows
    cumulative_source_rows = source_rows + len(inherited)
    cumulative_reconciled = accepted_rows + len(cumulative) == cumulative_source_rows
    external_checks = [
        QualityCheck(
            "calendar_repair_parent_reconciliation",
            parent_reconciled,
            {
                "source_rows": source_rows,
                "accepted_rows": accepted_rows,
                "newly_quarantined_rows": len(new_quarantine),
            },
            "accepted + new quarantine = source publication",
            "every source publication row has exactly one disposition",
        ),
        QualityCheck(
            "cumulative_quarantine_reconciliation",
            cumulative_reconciled,
            {
                "source_lineage_rows": cumulative_source_rows,
                "accepted_rows": accepted_rows,
                "cumulative_quarantine_rows": len(cumulative),
            },
            "accepted + cumulative quarantine = provider lineage",
            "all inherited and newly rejected rows remain accounted for",
        ),
        *coverage_bar_quarantine_checks(
            cumulative,
            source_row_count=cumulative_source_rows,
            security_universe=security_universe,
            target_session=source.target_session,
            max_ratio=float(settings.max_bar_quarantine_ratio),
            max_target_ratio=float(settings.max_target_bar_quarantine_ratio),
        ),
    ]
    store = BroadCoverageStore(
        catalog=catalog,
        lake_dir=CONFIG.abs_path(str(CONFIG.data.foundation.lake_dir)),
    )
    validation_checks, statistics = store._validate_partitions(
        accepted_paths,
        security_universe=security_universe,
        target_session=pd.Timestamp(source.target_session),
        min_target_coverage=float(settings.min_target_coverage),
    )
    passed = all(
        check.passed for check in [*validation_checks, *external_checks]
    )
    quality_lineage = {
        "policy": "PROVIDER_BAD_BAR_AND_XNYS_CALENDAR_QUARANTINE_V2",
        "parent_dataset_version_id": source.version_id,
        "parent_dataset_manifest_sha256": source.manifest_checksum_sha256,
        "source_publication_row_count": source_rows,
        "accepted_row_count": accepted_rows,
        "inherited_quarantine_row_count": len(inherited),
        "newly_quarantined_row_count": len(new_quarantine),
        "cumulative_quarantine_row_count": len(cumulative),
        "quarantine_sha256": _sha256(quarantine_path),
        "inherited_quarantine_lineage": inherited_lineage,
    }
    publication = None
    if args.publish and passed:
        publication = store.publish_partitions(
            accepted_paths,
            security_universe=security_universe,
            target_session=source.target_session,
            security_master=security_generation,
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
        "source_version_id": source.version_id,
        "target_session": source.target_session.isoformat(),
        "security_master_generation_id": security_generation.generation_id,
        "source_row_count": source_rows,
        "accepted_row_count": accepted_rows,
        "inherited_quarantine_row_count": len(inherited),
        "newly_quarantined_row_count": len(new_quarantine),
        "cumulative_quarantine_row_count": len(cumulative),
        "new_quarantine_reason_counts": new_quarantine["quality_reasons"].value_counts().to_dict(),
        "quarantine_path": str(quarantine_path),
        "quarantine_sha256": _sha256(quarantine_path),
        "partitions": partitions,
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
            f"{result['status']} source={result['source_version_id']} "
            f"accepted={result['accepted_row_count']} "
            f"new_quarantine={result['newly_quarantined_row_count']}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
