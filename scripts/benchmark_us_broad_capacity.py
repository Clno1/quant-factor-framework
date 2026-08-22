#!/usr/bin/env python3
"""Benchmark the proposed broad-factor shape on the fixed SG host.

The benchmark uses deterministic synthetic observations. Each universe size is
executed in an isolated subprocess so peak RSS is attributable to that size.
Temporary Parquet and DuckDB spill files are deleted after every worker.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

import duckdb


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.io import atomic_save_json  # noqa: E402


SCHEMA_VERSION = 1
DEFAULT_OUTPUT_DIR = ROOT / "outputs" / "data_audits"
DEFAULT_SCRATCH_DIR = ROOT / "tmp"


def _parse_sizes(raw: str) -> list[int]:
    sizes = list(dict.fromkeys(int(value.strip()) for value in raw.split(",")))
    if not sizes or any(value < 1 for value in sizes):
        raise ValueError("sizes must contain positive integers")
    return sizes


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark US broad-factor storage and research capacity",
    )
    parser.add_argument("--sizes", default="100,500,3000")
    parser.add_argument("--sessions", type=int, default=1900)
    parser.add_argument("--memory-limit-mb", type=int, default=600)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--scratch-dir", default=str(DEFAULT_SCRATCH_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--securities", type=int, default=None, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def _rss_mb() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw / (1024.0 * 1024.0) if sys.platform == "darwin" else raw / 1024.0


def _timed(operation: Callable[[], Any]) -> tuple[Any, float]:
    started = time.perf_counter()
    result = operation()
    return result, round(time.perf_counter() - started, 3)


def _scalar(connection: duckdb.DuckDBPyConnection, query: str) -> Any:
    return connection.execute(query).fetchone()[0]


def _run_worker(
    *,
    securities: int,
    sessions: int,
    memory_limit_mb: int,
    threads: int,
    scratch_dir: Path,
) -> dict[str, Any]:
    if securities < 1 or sessions < 300:
        raise ValueError("worker requires securities > 0 and sessions >= 300")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    worker_started = time.perf_counter()
    with tempfile.TemporaryDirectory(
        prefix=f"broad_capacity_{securities}_",
        dir=scratch_dir,
    ) as temporary:
        temporary_path = Path(temporary)
        parquet_path = temporary_path / "factor_observations.parquet"
        spill_path = temporary_path / "spill"
        spill_path.mkdir()
        connection = duckdb.connect(":memory:")
        connection.execute(f"SET threads={int(threads)}")
        connection.execute(f"SET memory_limit='{int(memory_limit_mb)}MB'")
        escaped_spill = str(spill_path).replace("'", "''")
        connection.execute(f"SET temp_directory='{escaped_spill}'")

        escaped_parquet = str(parquet_path).replace("'", "''")
        write_query = f"""
            COPY (
              SELECT
                DATE '2019-01-02' + CAST(d AS INTEGER) AS date,
                printf('SEC_%06d', s) AS security_id,
                printf('S%06d', s) AS ticker,
                CAST(25.0 + (s % 400) * 0.25 + sin(d * 0.017 + s * 0.003)
                  AS DOUBLE) AS close,
                CAST(24.9 + (s % 400) * 0.25 + sin(d * 0.017 + s * 0.003)
                  AS DOUBLE) AS open,
                CAST(100000 + (s % 200) * 10000 + (d % 17) * 1000
                  AS DOUBLE) AS volume,
                CAST(sin(d * 0.013 + s * 0.019) AS DOUBLE) AS raw_value,
                CAST(sin(d * 0.013 + s * 0.019) + (s % 13) * 0.001
                  AS DOUBLE) AS clean_value,
                CAST(sin((d + 1) * 0.011 + s * 0.023) * 0.02
                  AS DOUBLE) AS next_return,
                printf('SECTOR_%02d', s % 20) AS sector,
                TRUE AS pit_member
              FROM range({int(sessions)}) AS dates(d)
              CROSS JOIN range({int(securities)}) AS securities(s)
            ) TO '{escaped_parquet}' (
              FORMAT PARQUET,
              COMPRESSION ZSTD,
              ROW_GROUP_SIZE 100000
            )
        """
        _, write_seconds = _timed(lambda: connection.execute(write_query))
        parquet_bytes = parquet_path.stat().st_size
        source = f"read_parquet('{escaped_parquet}')"
        last_date = _scalar(connection, f"SELECT max(date) FROM {source}")
        first_security = "SEC_000000"

        snapshot_query = f"""
            SELECT ticker, raw_value, clean_value,
                   rank() OVER (ORDER BY clean_value DESC) AS factor_rank,
                   percent_rank() OVER (ORDER BY clean_value) * 100.0 AS percentile
            FROM {source}
            WHERE date = DATE '{last_date}' AND pit_member
            ORDER BY factor_rank, ticker
            LIMIT 100
        """
        snapshot_rows, snapshot_seconds = _timed(
            lambda: len(connection.execute(snapshot_query).fetchall())
        )

        history_query = f"""
            SELECT date, raw_value, clean_value
            FROM {source}
            WHERE security_id = '{first_security}'
            ORDER BY date
        """
        history_rows, history_seconds = _timed(
            lambda: len(connection.execute(history_query).fetchall())
        )

        incremental_query = f"""
            WITH windowed AS (
              SELECT *,
                     close / lag(close, 21) OVER (
                       PARTITION BY security_id ORDER BY date
                     ) - 1.0 AS mom_1m,
                     close / lag(close, 273) OVER (
                       PARTITION BY security_id ORDER BY date
                     ) - 1.0 AS mom_12m,
                     stddev_samp(next_return) OVER (
                       PARTITION BY security_id ORDER BY date
                       ROWS BETWEEN 59 PRECEDING AND CURRENT ROW
                     ) AS vol_60d,
                     avg(close * volume) OVER (
                       PARTITION BY security_id ORDER BY date
                       ROWS BETWEEN 19 PRECEDING AND CURRENT ROW
                     ) AS adv20
              FROM {source}
              WHERE date >= DATE '{last_date}' - INTERVAL 280 DAY
            )
            SELECT count(*), avg(mom_1m), avg(mom_12m), avg(vol_60d), avg(adv20)
            FROM windowed
            WHERE date = DATE '{last_date}'
        """
        incremental_result, incremental_seconds = _timed(
            lambda: connection.execute(incremental_query).fetchone()
        )

        ic_query = f"""
            WITH ranked AS (
              SELECT date,
                     rank() OVER (
                       PARTITION BY date ORDER BY clean_value
                     )::DOUBLE AS factor_rank,
                     rank() OVER (
                       PARTITION BY date ORDER BY next_return
                     )::DOUBLE AS return_rank
              FROM {source}
              WHERE pit_member
            ), daily AS (
              SELECT date, corr(factor_rank, return_rank) AS ic
              FROM ranked
              GROUP BY date
            )
            SELECT count(*), avg(ic), stddev_samp(ic)
            FROM daily
        """
        ic_result, ic_seconds = _timed(
            lambda: connection.execute(ic_query).fetchone()
        )

        quintile_query = f"""
            WITH assigned AS (
              SELECT date, next_return,
                     ntile(5) OVER (
                       PARTITION BY date ORDER BY clean_value
                     ) AS quintile
              FROM {source}
              WHERE pit_member
            ), daily AS (
              SELECT date, quintile, avg(next_return) AS group_return
              FROM assigned
              GROUP BY date, quintile
            )
            SELECT count(*), avg(group_return)
            FROM daily
        """
        quintile_result, quintile_seconds = _timed(
            lambda: connection.execute(quintile_query).fetchone()
        )
        spill_bytes = sum(
            path.stat().st_size for path in spill_path.rglob("*") if path.is_file()
        )
        connection.close()

    return {
        "status": "PASS",
        "securities": int(securities),
        "sessions": int(sessions),
        "observation_rows": int(securities * sessions),
        "memory_limit_mb": int(memory_limit_mb),
        "threads": int(threads),
        "parquet_bytes": int(parquet_bytes),
        "estimated_eight_factor_bytes": int(parquet_bytes * 8),
        "spill_bytes": int(spill_bytes),
        "peak_rss_mb": round(_rss_mb(), 3),
        "duration_seconds": round(time.perf_counter() - worker_started, 3),
        "stages": {
            "parquet_write_seconds": write_seconds,
            "snapshot_query_seconds": snapshot_seconds,
            "history_query_seconds": history_seconds,
            "incremental_window_seconds": incremental_seconds,
            "full_history_ic_seconds": ic_seconds,
            "full_history_quintile_seconds": quintile_seconds,
        },
        "assertions": {
            "snapshot_rows": int(snapshot_rows),
            "history_rows": int(history_rows),
            "incremental_rows": int(incremental_result[0]),
            "ic_sessions": int(ic_result[0]),
            "quintile_rows": int(quintile_result[0]),
        },
    }


def _worker_command(args: argparse.Namespace, securities: int) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--securities", str(securities),
        "--sessions", str(args.sessions),
        "--memory-limit-mb", str(args.memory_limit_mb),
        "--threads", str(args.threads),
        "--scratch-dir", str(args.scratch_dir),
        "--json",
    ]


def _run_isolated_worker(args: argparse.Namespace, securities: int) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            _worker_command(args, securities),
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=int(args.timeout_seconds),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "FAIL",
            "securities": int(securities),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error_type": "TimeoutExpired",
            "error": f"worker exceeded {int(args.timeout_seconds)} seconds",
        }
    if completed.returncode != 0:
        return {
            "status": "FAIL",
            "securities": int(securities),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error_type": "WorkerFailed",
            "error": (completed.stderr or completed.stdout)[-2000:],
        }
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "FAIL",
            "securities": int(securities),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "error_type": type(exc).__name__,
            "error": completed.stdout[-2000:],
        }


def _write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"us_broad_capacity_{stamp}.json"
    atomic_save_json(report, path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    digest_path = path.with_suffix(path.suffix + ".sha256")
    digest_path.write_text(f"{digest}  {path.name}\n", encoding="ascii")
    return path, digest_path, digest


def _parent(args: argparse.Namespace) -> int:
    sizes = _parse_sizes(args.sizes)
    started = time.perf_counter()
    results = [_run_isolated_worker(args, size) for size in sizes]
    passed = all(result.get("status") == "PASS" for result in results)
    largest = results[-1] if results else {}
    within_memory = float(largest.get("peak_rss_mb", float("inf"))) <= 900.0
    decision = "GO_TO_SECURITY_MASTER" if passed and within_memory else "BLOCKED"
    report = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "US_BROAD_SYNTHETIC_CAPACITY",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host_contract": {
            "cpu_vcpus": 2,
            "physical_memory_gib": 1.9,
            "background_process_memory_max_mb": 900,
        },
        "parameters": {
            "sizes": sizes,
            "sessions": int(args.sessions),
            "duckdb_memory_limit_mb": int(args.memory_limit_mb),
            "threads": int(args.threads),
        },
        "results": results,
        "decision": decision,
        "limitations": [
            "Synthetic values validate shape and resource behavior, not FMP throughput.",
            "The full-history aggregate is one-factor equivalent; production runs factors serially.",
            "The quintile query validates grouped-return pressure, not the final execution-cost engine.",
        ],
        "duration_seconds": round(time.perf_counter() - started, 3),
    }
    path, digest_path, digest = _write_report(report, Path(args.output_dir))
    output = {
        "decision": decision,
        "report_path": str(path),
        "sha256_path": str(digest_path),
        "sha256": digest,
        "duration_seconds": report["duration_seconds"],
        "largest_peak_rss_mb": largest.get("peak_rss_mb"),
    }
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(
            "decision={decision} report={report_path} peak_rss_mb="
            "{largest_peak_rss_mb}".format(**output)
        )
    return 0 if decision == "GO_TO_SECURITY_MASTER" else 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.sessions < 300:
        raise ValueError("sessions must be at least 300")
    if args.memory_limit_mb < 256:
        raise ValueError("memory-limit-mb must be at least 256")
    if not 1 <= args.threads <= 2:
        raise ValueError("threads must be 1 or 2 on the fixed SG host")
    if args.worker:
        if args.securities is None:
            raise ValueError("worker requires --securities")
        result = _run_worker(
            securities=int(args.securities),
            sessions=int(args.sessions),
            memory_limit_mb=int(args.memory_limit_mb),
            threads=int(args.threads),
            scratch_dir=Path(args.scratch_dir),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
