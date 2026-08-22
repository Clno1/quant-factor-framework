#!/usr/bin/env python3
"""Collect one operational snapshot without sending external notifications."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.operations.registry import OperationsRegistry  # noqa: E402
from src.operations.watchdog import OperationsWatchdog  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/operations.yaml")
    parser.add_argument("--no-systemd", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    registry = OperationsRegistry(args.config)
    report = OperationsWatchdog(
        registry,
        inspect_systemd=False if args.no_systemd else None,
    ).run_once()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"operations watchdog: {report['status']} "
            f"jobs={report['jobs_observed']} incidents={report['incidents_open']} "
            f"duration={report['duration_seconds']:.3f}s"
        )
        print(f"snapshot: {report['snapshot_path']}")
    return 0 if report["status"] in {"SUCCESS", "DEGRADED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
