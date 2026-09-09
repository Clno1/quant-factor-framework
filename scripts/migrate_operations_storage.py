#!/usr/bin/env python3
"""Initialize or verify the operations ledger and read-only snapshot."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.operations.registry import OperationsRegistry  # noqa: E402
from src.operations.store import OperationsReader, OperationsStore, utc_now_iso  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("init", "verify"))
    parser.add_argument("--config", default="configs/operations.yaml")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    registry = OperationsRegistry(args.config)
    settings = registry.settings
    store = OperationsStore(settings.database_path, settings.snapshot_path)
    store.initialize()
    store.sync_job_definitions(registry.list(), observed_at=utc_now_iso())
    if args.mode == "init":
        store.publish_snapshot()
    report = store.integrity_report()
    report["snapshot_exists"] = settings.snapshot_path.is_file()
    report["snapshot_readable"] = False
    if report["snapshot_exists"]:
        try:
            OperationsReader(settings.snapshot_path).overview()
            report["snapshot_readable"] = True
        except Exception as exc:  # noqa: BLE001
            report["snapshot_error"] = str(exc)
    report["passed"] = bool(
        report["passed"] and report["snapshot_exists"] and report["snapshot_readable"]
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"operations storage: {'PASS' if report['passed'] else 'FAIL'} "
            f"jobs={report['counts']['job_definitions']} "
            f"snapshot={report['snapshot_path']}"
        )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
