#!/usr/bin/env python3
"""Repeatable local GA1-19 benchmark for Stage-1 group analytics.

The benchmark is deliberately offline.  It uses one deterministic 500-member
synthetic SP500 fixture, publishes real Parquet artifact bundles, then exercises
the same immutable-artifact readers and payload builders used by the web API.
Stdout is always one strict JSON document; a threshold failure exits with 1 and
an execution error exits with 2.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src.group_analytics.aggregation import aggregate_groups  # noqa: E402
from src.group_analytics.artifacts import (  # noqa: E402
    ArtifactReader,
    FileGroupArtifactStore,
    canonical_json_bytes,
)
from src.group_analytics.models import (  # noqa: E402
    ArtifactCombination,
    GroupAnalyticsBundle,
    RunStatus,
)
from src.group_analytics.service import GroupAnalyticsService  # noqa: E402
from src.group_analytics.settings import (  # noqa: E402
    GroupAnalyticsSettings,
    RankingSettings,
)
from src.webapp import group_analytics_routes  # noqa: E402


ASOF = "2026-07-15"
MEMBER_COUNT = 500
GROUP_COUNT = 11
SNAPSHOT_RUNS = 20
API_RUNS = 100
THRESHOLDS_MS = {
    "aggregate_snapshot": 5_000.0,
    "latest_heat_payload": 300.0,
    "detail_payload": 500.0,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--keep-artifacts",
        type=Path,
        help="Write benchmark artifacts here instead of a temporary directory.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Indent the otherwise compact strict-JSON result.",
    )
    return parser


def _synthetic_members() -> pd.DataFrame:
    """Build the frozen, deterministic 500-member synthetic SP500 input."""
    rows: list[dict[str, Any]] = []
    for index in range(MEMBER_COUNT):
        group_index = index % GROUP_COUNT
        ticker = f"S{index:03d}"
        # Deterministic cross-section with a group signal and member dispersion.
        group_signal = (group_index - (GROUP_COUNT - 1) / 2.0) * 0.0015
        member_signal = float(np.sin(index * 0.37) * 0.009)
        micro_signal = ((index % 17) - 8) * 0.00013
        rows.append(
            {
                "group_id": f"s{group_index + 1:03d}",
                "group_name": f"Synthetic Sector {group_index + 1:02d}",
                "level": "sector",
                "security_id": f"security:{ticker}",
                "counting_unit_id": f"security:{ticker}",
                "ticker": ticker,
                "name": f"Synthetic SP500 Member {index:03d}",
                "raw_return_1d": group_signal + member_signal + micro_signal,
                "is_valid_for_headline": True,
                "reason_codes": [],
            }
        )
    return pd.DataFrame(rows)


def _fixture_hash(frame: pd.DataFrame) -> str:
    normalized = frame.copy()
    normalized["reason_codes"] = normalized["reason_codes"].map(
        lambda values: ",".join(str(value) for value in values)
    )
    row_hashes = pd.util.hash_pandas_object(normalized, index=False).to_numpy()
    return "sha256:" + hashlib.sha256(row_hashes.tobytes()).hexdigest()


def _bundle(
    source: pd.DataFrame,
    *,
    settings: GroupAnalyticsSettings,
    run_id: str,
) -> GroupAnalyticsBundle:
    result = aggregate_groups(
        source,
        settings=settings,
        benchmark_return_1d=0.001,
    )
    GroupAnalyticsService._enrich_frames(
        result,
        run_id=run_id,
        asof=ASOF,
        explicit_research=False,
    )
    return GroupAnalyticsBundle(
        metrics=result.metrics,
        members=result.members,
        contributions=result.contributions,
        diagnostics={
            "missing_members": [],
            "low_confidence_groups": [],
            "classification_diagnostics": [],
            "benchmark_fixture": True,
        },
        manifest={
            "asof": ASOF,
            "generated_at": f"{ASOF}T21:00:00Z",
            "source_max_date": ASOF,
            "snapshot_time": f"{ASOF}T20:00:00Z",
            "snapshot_id": "EOD",
            "session_status": "FINAL",
            "freshness_status": "FRESH",
            "quality_status": "OK",
            "input_fingerprint": "sha256:benchmark-fixture-v1",
            "universe_version": "synthetic-sp500-500-v1",
            "taxonomy_version": "synthetic-fmp-sector-v1",
            "classification_asof": ASOF,
            "classification_hash": "sha256:synthetic-classification-v1",
            "classification_provider": "DETERMINISTIC_SYNTHETIC_FIXTURE",
            "group_id_mapping_version": "synthetic-group-ids-v1",
            "benchmark": "SPY",
            "counting_unit": "security_with_overrides",
            "issuer_dedupe_status": "NONE",
            "issuer_overrides_applied": False,
            "issuer_override_count": 0,
            "pit_universe_applied": False,
            "pit_classification_applied": False,
        },
        run={
            "asof": ASOF,
            "freshness_status": "FRESH",
            "quality_status": "OK",
            "reason_codes": [],
            "benchmark_fixture": True,
        },
    )


def _measure(repetitions: int, operation: Callable[[int], None]) -> list[float]:
    durations_ms: list[float] = []
    for iteration in range(repetitions):
        started = time.perf_counter_ns()
        operation(iteration)
        elapsed_ns = time.perf_counter_ns() - started
        durations_ms.append(elapsed_ns / 1_000_000.0)
    return durations_ms


def _summary(
    samples_ms: list[float],
    *,
    threshold_ms: float,
    condition: str,
) -> dict[str, Any]:
    p50 = float(np.percentile(samples_ms, 50, method="linear"))
    p95 = float(np.percentile(samples_ms, 95, method="linear"))
    maximum = max(samples_ms)
    return {
        "condition": condition,
        "iterations": len(samples_ms),
        "percentile_method": "numpy.percentile(method=linear)",
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(maximum, 3),
        "threshold_p95_ms": threshold_ms,
        "passed": p95 <= threshold_ms,
        "samples_ms": [round(value, 3) for value in samples_ms],
    }


def _dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for package in ("numpy", "pandas", "pyarrow", "fastapi"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    return versions


def _cpu_model() -> str | None:
    processor = platform.processor().strip()
    if processor:
        return processor
    if platform.system() == "Darwin":
        try:
            completed = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            return completed.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.casefold().startswith("model name"):
                return line.split(":", 1)[-1].strip() or None
    except OSError:
        pass
    return None


def _physical_memory_mb() -> float | None:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return round((pages * page_size) / (1024 * 1024), 1)
    except (OSError, ValueError):
        return None


def _peak_rss_mb() -> float:
    value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # Darwin reports bytes; Linux and most BSDs report KiB.
    divisor = 1024 * 1024 if platform.system() == "Darwin" else 1024
    return round(value / divisor, 1)


def _environment() -> dict[str, Any]:
    local_now = datetime.now().astimezone()
    return {
        "hostname": platform.node(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": _cpu_model(),
        "logical_cpu_count": os.cpu_count(),
        "physical_memory_mb": _physical_memory_mb(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "dependencies": _dependency_versions(),
        "timezone": str(local_now.tzinfo),
    }


def _run(output_root: Path) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    source = _synthetic_members()
    if len(source) != MEMBER_COUNT or source["group_id"].nunique() != GROUP_COUNT:
        raise RuntimeError("deterministic benchmark fixture has an invalid shape")

    settings = GroupAnalyticsSettings(
        enabled=True,
        web_enabled=True,
        output_root=output_root,
        ranking=RankingSettings(top_n=5, bottom_n=5),
    )
    combination = ArtifactCombination("SP500", "FMP", "sector", "eod")
    store = FileGroupArtifactStore(settings)

    def aggregate_and_publish(iteration: int) -> None:
        run_id = f"ga1_benchmark_{iteration:03d}"
        outcome = store.publish(
            run_id=run_id,
            combination=combination,
            bundle=_bundle(source, settings=settings, run_id=run_id),
        )
        if outcome.status != RunStatus.SUCCESS or not outcome.published:
            raise RuntimeError(f"benchmark publication failed: {run_id}")

    snapshot_samples = _measure(SNAPSHOT_RUNS, aggregate_and_publish)
    reader = ArtifactReader(settings)
    detail_group_id = str(source["group_id"].iloc[0])

    original_route_settings = group_analytics_routes.settings
    group_analytics_routes.settings = settings
    try:
        def heat_operation(_: int) -> None:
            loaded = reader.load_latest(combination)
            last_attempt = reader.load_last_attempt(combination)
            payload = group_analytics_routes._heat_payload(
                loaded,
                last_attempt,
                view="all",
                sort_by="robust_ew_return_1d",
                sort_order="desc",
                view_min_members=1,
                show_low_confidence=True,
                limit=None,
            )
            canonical_json_bytes(payload)

        def detail_operation(_: int) -> None:
            loaded = reader.load_latest(combination)
            payload = group_analytics_routes._detail_payload(
                loaded,
                group_id=detail_group_id,
                page=1,
                page_size=50,
                member_sort_by="headline_contribution",
                member_sort_order="desc",
            )
            canonical_json_bytes(payload)

        # GA1-19 calls for warm API measurements.  Exactly one untimed call
        # primes imports, parquet metadata and filesystem caches for each path.
        heat_operation(-1)
        heat_samples = _measure(API_RUNS, heat_operation)
        detail_operation(-1)
        detail_samples = _measure(API_RUNS, detail_operation)
    finally:
        group_analytics_routes.settings = original_route_settings

    results = {
        "aggregate_snapshot": _summary(
            snapshot_samples,
            threshold_ms=THRESHOLDS_MS["aggregate_snapshot"],
            condition=(
                "continuous in-process runs; no untimed warmup; each iteration "
                "re-aggregates 500 members and atomically publishes/validates a "
                "new immutable Parquet snapshot"
            ),
        ),
        "latest_heat_payload": _summary(
            heat_samples,
            threshold_ms=THRESHOLDS_MS["latest_heat_payload"],
            condition=(
                "warm; one untimed warmup; each iteration resolves and validates "
                "latest, reads all Parquet files, builds heat payload and strictly "
                "serializes JSON"
            ),
        ),
        "detail_payload": _summary(
            detail_samples,
            threshold_ms=THRESHOLDS_MS["detail_payload"],
            condition=(
                "warm; one untimed warmup; each iteration resolves and validates "
                "latest, reads all Parquet files, builds a 50-row detail payload "
                "and strictly serializes JSON"
            ),
        ),
    }
    passed = all(bool(value["passed"]) for value in results.values())
    finished_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "benchmark": "GA1-19-stage1-group-analytics",
        "benchmark_version": 1,
        "status": "PASS" if passed else "FAIL",
        "passed": passed,
        "started_at": started_at,
        "finished_at": finished_at,
        "scope": "local engineering benchmark; not a Singapore production-host retest",
        "fixture": {
            "kind": "deterministic synthetic SP500",
            "member_count": MEMBER_COUNT,
            "group_count": GROUP_COUNT,
            "asof": ASOF,
            "hash": _fixture_hash(source),
            "external_network_calls": 0,
        },
        "environment": {
            **_environment(),
            "process_peak_rss_mb": _peak_rss_mb(),
        },
        "results": results,
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.keep_artifacts is not None:
            args.keep_artifacts.mkdir(parents=True, exist_ok=True)
            payload = _run(args.keep_artifacts.resolve())
        else:
            with tempfile.TemporaryDirectory(prefix="ga1-benchmark-") as directory:
                payload = _run(Path(directory))
        exit_code = 0 if payload["passed"] else 1
    except Exception as exc:  # stdout remains machine-readable on every outcome
        payload = {
            "benchmark": "GA1-19-stage1-group-analytics",
            "benchmark_version": 1,
            "status": "ERROR",
            "passed": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        exit_code = 2
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            allow_nan=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
