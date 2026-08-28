#!/usr/bin/env python3
"""Publish core research universes with a narrow semantic-drift recovery."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.io import atomic_save_json  # noqa: E402
from src.data.semantic_recovery import is_recoverable_semantic_drift  # noqa: E402


DEFAULT_UNIVERSES = ("SP500", "NASDAQ100", "MAG7")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", action="append", dest="universes")
    parser.add_argument("--target-session")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--env-file")
    parser.add_argument(
        "--report-dir",
        default="outputs/data_audits/core_market_data",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _decode_json(text: str) -> dict[str, Any] | None:
    payload = str(text or "").strip()
    decoder = json.JSONDecoder()
    for position in reversed(
        [index for index, character in enumerate(payload) if character == "{"]
    ):
        try:
            value, end = decoder.raw_decode(payload[position:])
        except json.JSONDecodeError:
            continue
        if not payload[position + end :].strip() and isinstance(value, dict):
            return value
    return None


def _semantic_drift_failures(payload: dict[str, Any] | None) -> list[str]:
    if not isinstance(payload, dict):
        return []
    failures = {str(value).upper() for value in payload.get("failures") or []}
    if not failures:
        return []
    recoverable: list[str] = []
    for result in payload.get("results") or []:
        universe = str(result.get("universe") or "").upper()
        if universe not in failures:
            continue
        error = str(result.get("error") or "").lower()
        if is_recoverable_semantic_drift(error):
            recoverable.append(universe)
    return sorted(recoverable) if set(recoverable) == failures else []


def _result_target_session(payload: dict[str, Any] | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    sessions = {
        str(result.get("target_session") or "").strip()
        for result in payload.get("results") or []
        if str(result.get("target_session") or "").strip()
    }
    if len(sessions) == 1:
        return sessions.pop()
    return None


def _command(
    *,
    universes: list[str],
    target_session: str | None,
    workers: int,
    env_file: str | None,
    full_rebuild: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "run_data_pipeline.py"),
        "update",
    ]
    for universe in universes:
        command.extend(["--universe", universe])
    if target_session:
        command.extend(["--target-session", target_session])
    command.extend(["--workers", str(workers)])
    if env_file:
        command.extend(["--env-file", env_file])
    if full_rebuild:
        command.extend(["--force", "--full-rebuild"])
    command.append("--json")
    return command


def _run(command: list[str]) -> dict[str, Any]:
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    output_tail = ""
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="", flush=True)
        output_tail = (output_tail + line)[-2_000_000:]
    returncode = process.wait()
    return {
        "returncode": returncode,
        "command": command,
        "result": _decode_json(output_tail),
        "output_tail": output_tail[-4000:] or None,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    universes = list(dict.fromkeys(
        str(value).strip().upper()
        for value in (args.universes or DEFAULT_UNIVERSES)
        if str(value).strip()
    ))
    started_at = datetime.now(timezone.utc)
    incremental = _run(
        _command(
            universes=universes,
            target_session=args.target_session,
            workers=args.workers,
            env_file=args.env_file,
            full_rebuild=False,
        )
    )
    recovery_universes = (
        _semantic_drift_failures(incremental["result"])
        if incremental["returncode"] != 0
        else []
    )
    recovery = None
    if recovery_universes:
        print(
            "strict semantic drift detected; rebuilding only: "
            + ",".join(recovery_universes),
            file=sys.stderr,
            flush=True,
        )
        recovery = _run(
            _command(
                universes=recovery_universes,
                target_session=args.target_session,
                workers=args.workers,
                env_file=args.env_file,
                full_rebuild=True,
            )
        )
    success = incremental["returncode"] == 0 or (
        recovery is not None and recovery["returncode"] == 0
    )
    target = str(
        args.target_session
        or _result_target_session((recovery or incremental).get("result"))
        or "auto"
    )
    report = {
        "schema_version": 1,
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid4().hex[:8],
        "status": "SUCCESS" if success else "FAILED",
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "target_session": target,
        "universes": universes,
        "incremental": incremental,
        "semantic_drift_recovery_universes": recovery_universes,
        "recovery": recovery,
    }
    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = ROOT / report_dir
    report_path = report_dir / f"target={target}" / f"run={report['run_id']}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_save_json(report, report_path)
    report["report_path"] = str(report_path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
