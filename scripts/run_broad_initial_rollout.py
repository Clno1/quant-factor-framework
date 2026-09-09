#!/usr/bin/env python3
"""Run the resumable first publication of the broad US data chain.

This is deliberately separate from the daily incremental pipeline. It rebuilds
the current Security Master, resumes only exact coverage/factor checkpoints,
publishes PIT membership, creates a daily-compatible evidence report and records
the first exact shadow observation. It never enables the recurring timer.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import resource
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402
from src.utils.market_calendar import latest_publishable_xnys_session  # noqa: E402


EXPECTED_READINESS_BLOCKERS = {
    "PIT_CLASSIFICATION_POLICY",
    "PIT_INDUSTRY_COVERAGE",
}
CONFLICTING_SERVICES = (
    "quant-intraday-momentum-monitor.service",
    "quant-market-data.service",
    "quant-factor-research.service",
    "quant-paper-trading.service",
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-session")
    parser.add_argument("--env-file")
    parser.add_argument(
        "--report-dir",
        default="outputs/data_audits/broad_initial_rollout",
    )
    parser.add_argument("--skip-service-guard", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _decode_json(text: str) -> dict[str, Any] | None:
    payload = str(text or "").strip()
    if not payload:
        return None
    decoder = json.JSONDecoder()
    for position in reversed(
        [index for index, character in enumerate(payload) if character == "{"]
    ):
        try:
            value, end = decoder.raw_decode(payload[position:])
        except json.JSONDecodeError:
            continue
        if not payload[position + end:].strip() and isinstance(value, dict):
            return value
    return None


def _peak_rss_mb() -> float:
    values = [
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    ]
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return max(float(value) for value in values) / divisor


def _assert_services_inactive() -> None:
    active: list[str] = []
    for service in CONFLICTING_SERVICES:
        completed = subprocess.run(
            ["systemctl", "is-active", "--quiet", service],
            check=False,
        )
        if completed.returncode == 0:
            active.append(service)
    if active:
        raise RuntimeError(
            "conflicting production services are active: " + ", ".join(active)
        )


def _run_stage(
    name: str,
    command: list[str],
    *,
    accepted_codes: set[int] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        # Child progress is written to stderr. Inherit it so journalctl and the
        # operations site can observe long-running stages while they execute.
        stderr=None,
    )
    stdout, _ = process.communicate()
    if stdout:
        print(stdout, end="", flush=True)
    result = _decode_json(stdout)
    accepted = accepted_codes or {0}
    return {
        "name": name,
        "status": "SUCCESS" if process.returncode in accepted else "FAILED",
        "returncode": process.returncode,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "result": result,
        "stderr_tail": None,
    }


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    target = (
        str(args.target_session)
        if args.target_session
        else latest_publishable_xnys_session(
            delay_minutes=int(CONFIG.data.foundation.close_delay_minutes)
        ).date().isoformat()
    )
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid4().hex[:8]
    )
    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = ROOT / report_dir
    report_path = report_dir / f"target={target}" / f"run={run_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "RUNNING",
        "target_session": target,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stages": [],
    }

    def persist() -> None:
        report["updated_at"] = datetime.now(timezone.utc).isoformat()
        report["peak_rss_mb"] = round(_peak_rss_mb(), 3)
        atomic_save_json(report, report_path)

    def execute(
        name: str,
        command: list[str],
        *,
        accepted_codes: set[int] | None = None,
    ) -> dict[str, Any]:
        report["current_stage"] = name
        persist()
        stage = _run_stage(name, command, accepted_codes=accepted_codes)
        report["stages"].append(stage)
        persist()
        if stage["status"] != "SUCCESS":
            raise RuntimeError(f"{name} failed with exit code {stage['returncode']}")
        result = stage.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"{name} did not return its JSON contract")
        observed_target = str(result.get("target_session") or target)
        if observed_target != target:
            raise RuntimeError(
                f"{name} target differs: {observed_target} != {target}"
            )
        return result

    python = sys.executable
    env_args = ["--env-file", str(args.env_file)] if args.env_file else []
    started = time.perf_counter()
    try:
        if not args.skip_service_guard:
            _assert_services_inactive()
        execute(
            "RESOURCE_GUARD",
            [
                python,
                str(ROOT / "scripts" / "check_broad_resources.py"),
                "--minimum-memory-mb",
                "350",
                "--minimum-disk-gb",
                "15",
                "--json",
            ],
        )
        execute(
            "SECURITY_MASTER",
            [
                python,
                str(ROOT / "scripts" / "build_security_master.py"),
                "--target-session",
                target,
                *env_args,
                "--publish",
                "--json",
            ],
        )
        coverage = execute(
            "US_EQUITY_COVERAGE_BACKFILL",
            [
                python,
                str(ROOT / "scripts" / "backfill_us_equity_coverage.py"),
                "--target-session",
                target,
                *env_args,
                "--auto-resume",
                "--publish",
                "--json",
            ],
        )
        publication = coverage.get("publication") or {}
        coverage_version_id = publication.get("version_id")
        if not coverage_version_id:
            raise RuntimeError("coverage publication did not expose version_id")
        execute(
            "US_LIQUID_5M_PIT",
            [
                python,
                str(ROOT / "scripts" / "build_us_liquid_pit.py"),
                "--dataset-version-id",
                str(coverage_version_id),
                "--full-rebuild",
                "--publish",
                "--json",
            ],
        )
        execute(
            "DAILY_COMPATIBILITY_PIPELINE",
            [
                python,
                str(ROOT / "scripts" / "run_broad_daily_pipeline.py"),
                "--target-session",
                target,
                *env_args,
                "--json",
            ],
        )
        execute(
            "BROAD_FACTOR_DATA",
            [
                python,
                str(ROOT / "scripts" / "run_broad_factor_data.py"),
                "--auto-resume",
                "--publish",
                "--json",
            ],
        )
        readiness = execute(
            "BROAD_RESEARCH_READINESS",
            [
                python,
                str(ROOT / "scripts" / "check_broad_research_readiness.py"),
                "--json",
            ],
            accepted_codes={0, 2},
        )
        if readiness.get("status") != "READY":
            blockers = set(readiness.get("blockers") or [])
            if (
                "PIT_CLASSIFICATION_POLICY" not in blockers
                or blockers - EXPECTED_READINESS_BLOCKERS
            ):
                raise RuntimeError(
                    "readiness has unexpected blockers: "
                    + ", ".join(sorted(blockers))
                )
            report["expected_readiness_blockers"] = sorted(blockers)
        shadow = execute(
            "BROAD_SHADOW_OBSERVATION",
            [
                python,
                str(ROOT / "scripts" / "check_broad_shadow_observation.py"),
                "--record-current",
                "--json",
            ],
        )
        if str((shadow.get("last_attempt") or {}).get("status") or "") != "PASS":
            raise RuntimeError("first broad shadow observation did not pass")
        report["status"] = "SUCCESS"
        exit_code = 0
    except Exception as exc:  # noqa: BLE001
        report["status"] = "FAILED"
        report["error"] = str(exc)
        exit_code = 1
    report["current_stage"] = None
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    persist()
    report["report_path"] = str(report_path)
    return report, exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report, exit_code = run(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"broad initial rollout: {report['status']} "
            f"target={report['target_session']} report={report['report_path']}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
