#!/usr/bin/env python3
"""Build or publish the version-bound US_LIQUID_5M factor-data generation.

The command is candidate-only by default.  It runs one registered factor and
one calendar month at a time, persists a checkpoint after every partition and
can resume the exact same immutable input binding with ``--generation-id``.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import gc
import json
import os
from pathlib import Path
import resource
import sys
import time
import traceback
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.data.broad_coverage import BroadCoverageReader  # noqa: E402
from src.data.foundation import (  # noqa: E402
    DataFoundationError,
    MarketDataCatalog,
    MarketDataReader,
    QualityCheck,
)
from src.data.security_master import CLASSIFICATION_POLICY  # noqa: E402
from src.data.security_master_store import SecurityMasterStore  # noqa: E402
from src.data.universe_ids import US_EQUITY_COVERAGE, US_LIQUID_5M  # noqa: E402
from src.data.universe_publication import DerivedUniverseStore  # noqa: E402
from src.factors import get_factor  # noqa: E402
from src.factors.broad_pipeline import (  # noqa: E402
    BroadFactorCalculator,
    INPUT_FINGERPRINT_METHOD,
    factor_input_fingerprint,
    output_months,
)
from src.factors.data_publication import (  # noqa: E402
    FactorDataStore,
    FactorPartition,
)
from src.utils.file_lock import file_lock  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version-id")
    parser.add_argument("--universe-version-id")
    parser.add_argument("--generation-id")
    parser.add_argument(
        "--auto-resume",
        action="store_true",
        help="resume the only staging generation matching every immutable input",
    )
    parser.add_argument(
        "--restart-after-partitions",
        type=int,
        default=0,
        help=(
            "restart the worker after this many newly computed partitions; "
            "use 1 on memory-constrained production hosts"
        ),
    )
    parser.add_argument("--start")
    parser.add_argument(
        "--full-rebuild",
        action="store_true",
        help="Recompute every factor/month even when an authenticated partition is reusable.",
    )
    parser.add_argument(
        "--factor",
        action="append",
        dest="factors",
        help="Candidate-only subset. Formal publication always requires all configured factors.",
    )
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return float(value) / divisor


def _current_rss_mb() -> float:
    if sys.platform.startswith("linux"):
        resident_pages = int(Path("/proc/self/statm").read_text().split()[1])
        return resident_pages * resource.getpagesize() / (1024.0 * 1024.0)
    return _rss_mb()


def _release_partition_memory() -> None:
    """Return temporary Pandas/Arrow pages after each bounded partition."""
    gc.collect()
    try:
        import pyarrow as pa

        pa.default_memory_pool().release_unused()
    except (ImportError, AttributeError):
        pass
    gc.collect()
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None)
        malloc_trim = getattr(libc, "malloc_trim", None)
        if malloc_trim is not None:
            malloc_trim(0)


def _restart_from_checkpoint(generation_id: str) -> None:
    """Replace the worker so Linux releases all partition-local memory."""
    argv = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    if "--auto-resume" not in argv and "--generation-id" not in argv:
        argv.extend(["--generation-id", generation_id])
    os.execv(sys.executable, argv)


def _checkpoint_identity(
    *,
    generation_id: str,
    parent: Any,
    universe_version: Any,
    security_generation: Any,
    factors: list[str],
    start: pd.Timestamp,
    reuse_publication_id: str | None,
) -> dict[str, Any]:
    instances = {factor_id: get_factor(factor_id) for factor_id in factors}
    return {
        "schema_version": 2,
        "input_fingerprint_method": INPUT_FINGERPRINT_METHOD,
        "calculation_contract": json.loads(json.dumps({
            "preprocessing": dict(CONFIG.preprocessing),
            "factors": {
                factor_id: {
                    "module": instances[factor_id].__class__.__module__,
                    "class": instances[factor_id].__class__.__qualname__,
                    "parameters": dict(vars(instances[factor_id])),
                    "direction": int(instances[factor_id].direction),
                    "inputs": list(instances[factor_id].inputs),
                } for factor_id in factors
            },
        }, sort_keys=True, default=str)),
        "generation_id": generation_id,
        "parent_dataset_version_id": parent.version_id,
        "parent_dataset_manifest_sha256": parent.manifest_checksum_sha256,
        "universe_version_id": universe_version.universe_version_id,
        "membership_sha256": universe_version.membership_sha256,
        "eligibility_sha256": universe_version.eligibility_sha256,
        "security_master_generation_id": security_generation.generation_id,
        "security_master_sha256": security_generation.manifest_sha256,
        "target_session": parent.target_session.isoformat(),
        "start": start.date().isoformat(),
        "factors": factors,
        "reuse_publication_id": reuse_publication_id,
    }


def _load_checkpoint(path: Path, identity: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return {
            **identity,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "completed": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    mismatches = [
        key for key, value in identity.items() if payload.get(key) != value
    ]
    if mismatches:
        raise DataFoundationError(
            "broad factor checkpoint belongs to different immutable inputs: "
            f"{mismatches}"
        )
    if not isinstance(payload.get("completed"), dict):
        raise DataFoundationError("broad factor checkpoint is malformed")
    return payload


def _auto_resume_generation(
    factor_store: FactorDataStore,
    *,
    expected: dict[str, Any],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Find one exact incomplete factor generation without guessing."""
    matches: list[str] = []
    diagnostics: list[dict[str, Any]] = []
    for path in sorted(factor_store.output_root.glob(".staging_*/checkpoint.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            diagnostics.append({
                "checkpoint_path": str(path),
                "decision": "REJECT_MALFORMED",
            })
            continue
        mismatches = [
            key for key, value in expected.items()
            if payload.get(key) != value
        ]
        generation_id = str(payload.get("generation_id") or "")
        if not generation_id or path.parent.name != f".staging_{generation_id}":
            mismatches.append("generation_id")
        diagnostics.append({
            "checkpoint_path": str(path),
            "generation_id": generation_id or None,
            "decision": "MATCH" if not mismatches else "REJECT",
            "mismatches": sorted(set(mismatches)),
        })
        if not mismatches:
            matches.append(generation_id)
    if len(matches) > 1:
        raise DataFoundationError(
            "multiple exact factor checkpoints match current inputs: "
            + ", ".join(matches)
        )
    return (matches[0] if matches else None), diagnostics


def _audit_rows(
    factor_id: str,
    block_start: str,
    block_end: str,
    audit: dict[str, Any],
) -> list[dict[str, Any]]:
    neutralization = audit.get("neutralization") or {}
    neutral_daily = {
        str(row.get("date")): row
        for row in neutralization.get("daily") or []
    }
    rows: list[dict[str, Any]] = []
    for daily in audit.get("daily") or []:
        date_value = str(daily.get("date"))
        neutral = neutral_daily.get(date_value, {})
        rows.append({
            "factor_id": factor_id,
            "date": date_value,
            "block_start": block_start,
            "block_end": block_end,
            "raw_non_null": int(daily.get("raw_non_null") or 0),
            "clean_non_null": int(daily.get("clean_non_null") or 0),
            "winsorize_method": audit.get("winsorize_method"),
            "standardize_enabled": bool(audit.get("standardize_enabled")),
            "neutralization_applied": bool(neutral.get("applied", False)),
            "neutralization_reason": neutral.get("reason"),
            "known_industry": int(neutral.get("known_industry") or 0),
            "missing_industry": int(neutral.get("missing_industry") or 0),
        })
    return rows


def _quality_checks(
    *,
    state: dict[str, Any],
    factors: list[str],
    expected_periods: set[str],
) -> list[QualityCheck]:
    completed = state["completed"]
    settings = CONFIG.data.broad_factor_data
    observed_periods = {
        factor: {
            value["period"]
            for value in completed.values()
            if value["factor_id"] == factor
        }
        for factor in factors
    }
    complete = all(observed_periods[factor] == expected_periods for factor in factors)
    checks = [QualityCheck(
        "complete_factor_periods",
        complete,
        {key: len(value) for key, value in observed_periods.items()},
        {key: len(expected_periods) for key in factors},
        "all factor/month checkpoints are complete" if complete else "factor/month checkpoints are incomplete",
    )]
    fingerprints_complete = all(
        (value.get("partition") or {}).get("input_fingerprint_sha256")
        and (value.get("partition") or {}).get("input_fingerprint_method")
        == INPUT_FINGERPRINT_METHOD
        for value in completed.values()
    )
    checks.append(QualityCheck(
        "input_equivalence_fingerprints",
        bool(completed) and fingerprints_complete,
        sum(
            1
            for value in completed.values()
            if (value.get("partition") or {}).get("input_fingerprint_sha256")
        ),
        len(completed),
        "every factor partition is bound to its exact input fingerprint",
    ))
    for factor in factors:
        entries = [
            value for value in completed.values() if value["factor_id"] == factor
        ]
        entries.sort(key=lambda value: value["period"])
        latest = entries[-1]["diagnostics"] if entries else {}
        raw_coverage = float(latest.get("latest_raw_coverage") or 0.0)
        clean_coverage = float(latest.get("latest_clean_coverage") or 0.0)
        zero_count = sum(
            int(value["diagnostics"].get("zero_std_cross_sections") or 0)
            for value in entries
        )
        eligible_count = sum(
            int(value["diagnostics"].get("eligible_cross_sections") or 0)
            for value in entries
        )
        zero_ratio = zero_count / eligible_count if eligible_count else 1.0
        checks.extend([
            QualityCheck(
                f"{factor}_latest_raw_coverage",
                raw_coverage >= float(settings.minimum_latest_raw_coverage),
                raw_coverage,
                float(settings.minimum_latest_raw_coverage),
                f"latest warm-up-eligible raw coverage {raw_coverage:.2%}",
            ),
            QualityCheck(
                f"{factor}_latest_clean_coverage",
                clean_coverage >= float(settings.minimum_latest_clean_coverage),
                clean_coverage,
                float(settings.minimum_latest_clean_coverage),
                f"latest warm-up-eligible clean coverage {clean_coverage:.2%}",
            ),
            QualityCheck(
                f"{factor}_zero_std_cross_sections",
                zero_ratio
                <= float(settings.maximum_zero_std_cross_section_ratio),
                zero_ratio,
                float(settings.maximum_zero_std_cross_section_ratio),
                f"zero-std clean cross-section ratio {zero_ratio:.2%}",
            ),
        ])
        disappeared = sorted({
            ticker
            for value in entries
            for ticker in value.get("disappeared_tickers") or []
        })
        checks.append(QualityCheck(
            f"{factor}_unexplained_clean_disappearance",
            not disappeared,
            disappeared[:50],
            [],
            "no security lost every clean observation" if not disappeared else "raw securities disappeared during preprocessing",
        ))
    return checks


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    if args.auto_resume and args.generation_id:
        raise DataFoundationError(
            "--auto-resume and --generation-id are mutually exclusive"
        )
    if args.restart_after_partitions < 0:
        raise DataFoundationError("--restart-after-partitions cannot be negative")
    catalog = MarketDataCatalog(CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)))
    market_reader = MarketDataReader(catalog=catalog)
    parent = (
        market_reader.require_version(
            US_EQUITY_COVERAGE,
            args.dataset_version_id,
            require_price_semantics=True,
        )
        if args.dataset_version_id
        else market_reader.require_latest(
            US_EQUITY_COVERAGE,
            require_price_semantics=True,
        )
    )
    universe_settings = CONFIG.data.broad_universe
    universe_store = DerivedUniverseStore(
        catalog=catalog,
        snapshot_root=CONFIG.abs_path(str(universe_settings.snapshot_dir)),
        market_reader=market_reader,
    )
    universe_version = (
        universe_store.get(US_LIQUID_5M, args.universe_version_id)
        if args.universe_version_id
        else universe_store.require_latest(US_LIQUID_5M)
    )
    if universe_version is None:
        raise DataFoundationError("US_LIQUID_5M universe version does not exist")
    universe_store.verify(universe_version)
    if universe_version.parent_dataset_version_id != parent.version_id:
        raise DataFoundationError("coverage and PIT universe versions are not aligned")

    security_settings = CONFIG.data.security_master
    security_store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(security_settings.snapshot_dir)),
    )
    security_generation, security_frames = security_store.load_published()
    if (
        security_generation.generation_id
        != universe_version.security_master_generation_id
        or security_generation.manifest_sha256
        != universe_version.security_master_manifest_sha256
    ):
        raise DataFoundationError(
            "PIT universe is not bound to the currently published Security Master"
        )
    if parent.target_session != universe_version.target_session:
        raise DataFoundationError("coverage and PIT target sessions differ")

    configured_factors = [str(value).upper() for value in CONFIG.factors.enabled]
    factors = (
        list(dict.fromkeys(str(value).upper() for value in args.factors))
        if args.factors
        else configured_factors
    )
    unknown = sorted(set(factors) - set(configured_factors))
    if unknown:
        raise DataFoundationError(f"factors are not enabled: {unknown}")
    if args.publish and factors != configured_factors:
        raise DataFoundationError(
            "formal factor-data publication requires all configured factors"
        )
    start = pd.Timestamp(args.start or universe_settings.research_start).normalize()
    target = pd.Timestamp(parent.target_session).normalize()
    if start > target:
        raise DataFoundationError("factor-data start is after target session")
    periods = output_months(start, target)
    if not periods:
        raise DataFoundationError("factor-data range contains no XNYS sessions")

    factor_store = FactorDataStore(
        market_reader=market_reader,
        universe_store=universe_store,
    )
    previous_publication = None
    previous_audit = pd.DataFrame()
    previous_partitions: dict[tuple[str, int, int], FactorPartition] = {}
    if not args.full_rebuild and factor_store.publication_path.is_file():
        previous_publication = factor_store.load_publication(
            verify_partitions=False
        )
        previous_target = pd.Timestamp(
            previous_publication["target_session"]
        ).normalize()
        if previous_target > target:
            raise DataFoundationError(
                "existing factor-data publication is newer than requested inputs"
            )
        same_inputs = (
            previous_target == target
            and previous_publication.get("parent_dataset_version_id")
            == parent.version_id
            and previous_publication.get("universe_version_id")
            == universe_version.universe_version_id
            and previous_publication.get("security_master_generation_id")
            == security_generation.generation_id
            and set(previous_publication.get("factors") or {}) == set(factors)
        )
        if args.publish and same_inputs:
            factor_store.load_publication(verify_partitions=True)
            existing_report = (
                factor_store.generation_directory(
                    str(previous_publication["generation_id"])
                )
                / "run_report.json"
            )
            return {
                "status": "NOOP",
                "generation_id": previous_publication["generation_id"],
                "target_session": parent.target_session.isoformat(),
                "start": start.date().isoformat(),
                "factors": factors,
                "period_count": 0,
                "completed_partition_count": 0,
                "reused_partition_count": 0,
                "computed_partition_count": 0,
                "checks": [],
                "checkpoint_path": None,
                "publication": previous_publication,
                "report_path": (
                    str(existing_report) if existing_report.is_file() else None
                ),
                "started_at": started_at.isoformat(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "peak_rss_mb": round(_rss_mb(), 3),
            }, 0
        try:
            previous_audit = factor_store.preprocessing_audit(
                previous_publication
            )
        except DataFoundationError:
            # Older generations without a complete audit are valid read-only
            # inputs, but cannot be reused in a new formal generation.
            previous_audit = pd.DataFrame()
        for factor_id in factors:
            if factor_id not in (previous_publication.get("factors") or {}):
                continue
            for partition in factor_store.partition_entries(
                previous_publication, factor_id
            ):
                previous_partitions[
                    (factor_id, int(partition.year), int(partition.month))
                ] = partition
    resume_identity = {
        key: value
        for key, value in _checkpoint_identity(
            generation_id="DISCOVERY_ONLY",
            parent=parent,
            universe_version=universe_version,
            security_generation=security_generation,
            factors=factors,
            start=start,
            reuse_publication_id=(
                str(previous_publication["publication_id"])
                if previous_publication is not None else None
            ),
        ).items()
        if key != "generation_id"
    }
    auto_generation = None
    resume_diagnostics: list[dict[str, Any]] = []
    if args.auto_resume:
        auto_generation, resume_diagnostics = _auto_resume_generation(
            factor_store,
            expected=resume_identity,
        )
    generation_id = (
        args.generation_id
        or auto_generation
        or factor_store.new_generation_id()
    )
    staging = factor_store.staging_directory(generation_id)
    staging.mkdir(parents=True, exist_ok=True)
    checkpoint_path = staging / "checkpoint.json"
    identity = _checkpoint_identity(
        generation_id=generation_id,
        parent=parent,
        universe_version=universe_version,
        security_generation=security_generation,
        factors=factors,
        start=start,
        reuse_publication_id=(
            str(previous_publication["publication_id"])
            if previous_publication is not None
            else None
        ),
    )
    state = _load_checkpoint(checkpoint_path, identity)
    state["status"] = "RUNNING"
    state["expected_partition_count"] = len(factors) * len(periods)
    state["completed_partition_count"] = len(state["completed"])
    state["resume_diagnostics"] = resume_diagnostics
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save_json(state, checkpoint_path)
    membership = universe_store.load_membership(
        US_LIQUID_5M, version_id=universe_version.universe_version_id
    )
    calculator = BroadFactorCalculator(
        coverage_reader=BroadCoverageReader(market_reader=market_reader),
        parent_version=parent,
        membership=membership,
        master=security_frames["master"],
        classifications=security_frames["classifications"],
    )

    process_lock = CONFIG.abs_path(
        str(CONFIG.data.broad_factor_data.checkpoint_dir)
    ) / ".writer.lock"
    computed_in_process = 0
    with file_lock(process_lock):
        for factor_id in factors:
            for block_start, block_end in periods:
                period = block_start.to_period("M").strftime("%Y-%m")
                key = f"{factor_id}:{period}"
                completed = state["completed"].get(key)
                if completed is not None:
                    partition = FactorPartition(**completed["partition"])
                    if partition.source_generation_id == generation_id:
                        staged_path = factor_store.staged_partition_path(
                            partition, generation_id=generation_id
                        )
                        if staged_path.is_file():
                            import hashlib

                            digest = hashlib.sha256(staged_path.read_bytes()).hexdigest()
                            if digest == partition.sha256:
                                continue
                    else:
                        factor_store.verified_partition_paths([partition])
                        continue
                    raise DataFoundationError(
                        f"checkpointed partition is missing or changed: {key}"
                    )
                fingerprint, proof = factor_input_fingerprint(
                    factor_id=factor_id,
                    parent_version=parent,
                    membership=membership,
                    classifications=security_frames["classifications"],
                    output_start=block_start,
                    output_end=block_end,
                )
                previous_partition = previous_partitions.get(
                    (factor_id, block_start.year, block_start.month)
                )
                reusable = (
                    previous_partition is not None
                    and previous_partition.date_start
                    == block_start.date().isoformat()
                    and previous_partition.date_end
                    == block_end.date().isoformat()
                    and previous_partition.input_fingerprint_method
                    == INPUT_FINGERPRINT_METHOD
                    and previous_partition.input_fingerprint_sha256
                    == fingerprint
                    and previous_partition.latest_raw_coverage is not None
                    and previous_partition.latest_clean_coverage is not None
                    and previous_partition.zero_std_cross_sections is not None
                    and previous_partition.eligible_cross_sections is not None
                    and not previous_audit.empty
                    and {"factor_id", "date"}.issubset(previous_audit.columns)
                )
                if reusable:
                    factor_store.verified_partition_paths([previous_partition])
                    audit_dates = pd.to_datetime(
                        previous_audit["date"], errors="coerce"
                    ).dt.normalize()
                    reused_audit = previous_audit.loc[
                        previous_audit["factor_id"].astype(str).eq(factor_id)
                        & audit_dates.between(block_start, block_end)
                    ].copy()
                    if not reused_audit.empty:
                        reused_audit["date"] = pd.to_datetime(
                            reused_audit["date"]
                        ).dt.date.astype(str)
                        diagnostics = {
                            "factor_id": factor_id,
                            "output_start": block_start.date().isoformat(),
                            "output_end": block_end.date().isoformat(),
                            "latest_raw_coverage": previous_partition.latest_raw_coverage,
                            "latest_clean_coverage": previous_partition.latest_clean_coverage,
                            "zero_std_cross_sections": previous_partition.zero_std_cross_sections,
                            "eligible_cross_sections": previous_partition.eligible_cross_sections,
                        }
                        state["completed"][key] = {
                            "factor_id": factor_id,
                            "period": period,
                            "partition": previous_partition.to_dict(),
                            "diagnostics": diagnostics,
                            "disappeared_tickers": [],
                            "preprocessing_rows": reused_audit.to_dict("records"),
                            "reused": True,
                            "input_proof": proof,
                        }
                        state["updated_at"] = datetime.now(timezone.utc).isoformat()
                        state["completed_partition_count"] = len(
                            state["completed"]
                        )
                        atomic_save_json(state, checkpoint_path)
                        print(
                            f"[{factor_id}] reused authenticated {period}",
                            file=sys.stderr,
                            flush=True,
                        )
                        continue
                print(
                    f"[{factor_id}] computing {period} "
                    f"({block_start.date()}..{block_end.date()})",
                    file=sys.stderr,
                    flush=True,
                )
                result = calculator.compute_month(
                    factor_id,
                    output_start=block_start,
                    output_end=block_end,
                )
                partition = factor_store.write_partition(
                    result.observations,
                    generation_id=generation_id,
                    factor_id=factor_id,
                    target_session=target,
                    input_fingerprint_sha256=fingerprint,
                    input_fingerprint_method=INPUT_FINGERPRINT_METHOD,
                    diagnostics=result.diagnostics,
                )
                audit = result.preprocessing_audit.to_dict()
                state["completed"][key] = {
                    "factor_id": factor_id,
                    "period": period,
                    "partition": partition.to_dict(),
                    "diagnostics": result.diagnostics,
                    "disappeared_tickers": list(
                        result.preprocessing_audit.raw_non_null_clean_all_null_tickers
                    ),
                    "preprocessing_rows": _audit_rows(
                        factor_id,
                        result.output_start.isoformat(),
                        result.output_end.isoformat(),
                        audit,
                    ),
                    "reused": False,
                    "input_proof": proof,
                }
                state["updated_at"] = datetime.now(timezone.utc).isoformat()
                state["peak_rss_mb"] = _rss_mb()
                state["completed_partition_count"] = len(state["completed"])
                atomic_save_json(state, checkpoint_path)
                del result, audit
                _release_partition_memory()
                print(
                    f"[{factor_id}] completed {period} "
                    f"progress={state['completed_partition_count']}/"
                    f"{state['expected_partition_count']} "
                    f"current_rss_mb={_current_rss_mb():.1f}",
                    file=sys.stderr,
                    flush=True,
                )
                computed_in_process += 1
                if (
                    args.restart_after_partitions
                    and computed_in_process >= args.restart_after_partitions
                    and state["completed_partition_count"]
                    < state["expected_partition_count"]
                ):
                    print(
                        "[factor] restarting from authenticated checkpoint "
                        f"generation={generation_id} "
                        f"progress={state['completed_partition_count']}/"
                        f"{state['expected_partition_count']}",
                        file=sys.stderr,
                        flush=True,
                    )
                    _restart_from_checkpoint(generation_id)

    expected_periods = {
        start_value.to_period("M").strftime("%Y-%m")
        for start_value, _ in periods
    }
    checks = _quality_checks(
        state=state,
        factors=factors,
        expected_periods=expected_periods,
    )
    audit_rows = [
        row
        for value in state["completed"].values()
        for row in value.get("preprocessing_rows") or []
    ]
    audit_path = staging / "preprocessing_audit.parquet"
    pd.DataFrame(audit_rows).to_parquet(audit_path, index=False)
    factor_partitions = {
        factor: [
            FactorPartition(**value["partition"])
            for value in state["completed"].values()
            if value["factor_id"] == factor
        ]
        for factor in factors
    }
    factor_metadata = {}
    for factor_id in factors:
        factor = get_factor(factor_id)
        factor_metadata[factor_id] = {
            "direction": int(factor.direction),
            "factor_module": factor.__class__.__module__,
            "factor_class": factor.__class__.__qualname__,
            "factor_parameters": dict(vars(factor)),
        }
    failed_checks = [check for check in checks if not check.passed]
    state["status"] = "PASS" if not failed_checks else "FAIL"
    state["completed_partition_count"] = len(state["completed"])
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save_json(state, checkpoint_path)
    pointer = None
    if args.publish:
        pointer = factor_store.publish(
            generation_id=generation_id,
            universe=US_LIQUID_5M,
            parent_version=parent,
            universe_version=universe_version,
            security_master=security_generation,
            factor_partitions=factor_partitions,
            factor_metadata=factor_metadata,
            checks=checks,
            methodology_version=str(
                CONFIG.data.broad_factor_data.methodology_version
            ),
            preprocessing_methodology_version=str(
                CONFIG.data.broad_factor_data.preprocessing_methodology_version
            ),
            classification_policy=CLASSIFICATION_POLICY,
            preprocessing_audit_path=audit_path,
        )
    report = {
        "status": (
            "PUBLISHED"
            if pointer is not None
            else "CANDIDATE_PASS"
            if not failed_checks
            else "CANDIDATE_FAIL"
        ),
        "generation_id": generation_id,
        "target_session": parent.target_session.isoformat(),
        "start": start.date().isoformat(),
        "factors": factors,
        "period_count": len(periods),
        "completed_partition_count": len(state["completed"]),
        "reused_partition_count": sum(
            bool(value.get("reused")) for value in state["completed"].values()
        ),
        "computed_partition_count": sum(
            not bool(value.get("reused"))
            for value in state["completed"].values()
        ),
        "checks": [check.to_dict() for check in checks],
        "checkpoint_path": str(checkpoint_path),
        "resumed": bool(args.generation_id or auto_generation),
        "resume_diagnostics": resume_diagnostics,
        "publication": pointer,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mb": round(_rss_mb(), 3),
    }
    report_path = (
        factor_store.generation_directory(generation_id)
        if pointer is not None
        else staging
    ) / "run_report.json"
    atomic_save_json(report, report_path)
    report["report_path"] = str(report_path)
    return report, 0 if not failed_checks else 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        report, code = run(args)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        if args.json:
            print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"{report['status']} generation={report['generation_id']} "
            f"parts={report['completed_partition_count']} "
            f"elapsed={report['elapsed_seconds']}s"
        )
        print(report["report_path"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
