#!/usr/bin/env python3
"""Run the version-bound broad daily writer stages for one target session."""
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-session")
    parser.add_argument("--env-file")
    parser.add_argument(
        "--report-dir",
        default="outputs/data_audits/broad_daily_pipeline",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _decode_json(text: str) -> dict[str, Any] | None:
    payload = str(text or "").strip()
    if not payload:
        return None
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        # Project logging can precede a command's --json payload.  Scan from
        # the final object candidates and accept only one that consumes the
        # complete remaining stdout, never a nested object fragment.
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
    return value if isinstance(value, dict) else None


def _peak_rss_mb() -> float:
    values = [
        resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
    ]
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return max(float(value) for value in values) / divisor


def _run_stage(name: str, command: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="", flush=True)
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr, flush=True)
    return {
        "name": name,
        "status": "SUCCESS" if completed.returncode == 0 else "FAILED",
        "returncode": completed.returncode,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "result": _decode_json(completed.stdout),
        "stderr_tail": completed.stderr[-4000:] if completed.stderr else None,
        "command": command,
    }


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started_at = datetime.now(timezone.utc)
    target = (
        str(args.target_session)
        if args.target_session
        else latest_publishable_xnys_session(
            delay_minutes=int(CONFIG.data.foundation.close_delay_minutes)
        ).date().isoformat()
    )
    python = sys.executable
    env_arguments = ["--env-file", str(args.env_file)] if args.env_file else []
    commands = [
        (
            "SECURITY_MASTER",
            [
                python,
                str(ROOT / "scripts" / "build_security_master.py"),
                "--target-session",
                target,
                *env_arguments,
                "--publish",
                "--json",
            ],
        ),
        (
            "US_EQUITY_COVERAGE",
            [
                python,
                str(ROOT / "scripts" / "update_us_equity_coverage.py"),
                "--target-session",
                target,
                *env_arguments,
                "--publish",
                "--json",
            ],
        ),
    ]
    run_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid4().hex[:8]
    )
    started = time.perf_counter()
    stages: list[dict[str, Any]] = []
    for name, command in commands:
        stage = _run_stage(name, command)
        result = stage.get("result")
        if stage["returncode"] == 0 and (
            not isinstance(result, dict)
            or str(result.get("target_session") or "") != target
        ):
            stage["status"] = "FAILED"
            stage["returncode"] = 3
            stage["stderr_tail"] = (
                "child --json contract is missing or target_session differs"
            )
        stages.append(stage)
        if stage["returncode"] != 0:
            break
        if name == "US_EQUITY_COVERAGE":
            publication = result.get("publication") or {}
            coverage_version_id = (
                publication.get("version_id")
                if isinstance(publication, dict)
                else None
            ) or result.get("parent_dataset_version_id")
            if not coverage_version_id:
                stage["status"] = "FAILED"
                stage["returncode"] = 3
                stage["stderr_tail"] = (
                    "coverage JSON contract does not expose a version_id"
                )
                break
            commands.append((
                "US_LIQUID_5M_PIT",
                [
                    python,
                    str(ROOT / "scripts" / "build_us_liquid_pit.py"),
                    "--dataset-version-id",
                    str(coverage_version_id),
                    "--publish",
                    "--json",
                ],
            ))
    status = "SUCCESS" if len(stages) == len(commands) and all(
        stage["returncode"] == 0 for stage in stages
    ) else "FAILED"
    report = {
        "schema_version": 1,
        "run_id": run_id,
        "status": status,
        "target_session": target,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mb": round(_peak_rss_mb(), 3),
        "stages": stages,
    }
    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = ROOT / report_dir
    report_path = report_dir / f"target={target}" / f"run={run_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_save_json(report, report_path)
    report["report_path"] = str(report_path)
    return report, 0 if status == "SUCCESS" else 1


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report, exit_code = run(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"broad daily pipeline: {report['status']} "
            f"target={report['target_session']} report={report['report_path']}"
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
