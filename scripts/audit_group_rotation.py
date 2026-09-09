#!/usr/bin/env python3
"""Read-only reproducibility audit for a published rotation run."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.group_analytics.rotation.replay import replay_snapshot
from src.group_analytics.rotation.store import RotationStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="Default: latest successful run")
    parser.add_argument("--output-root", type=Path, help="Rotation artifact directory, not project output parent")
    args = parser.parse_args(argv)
    try:
        result = replay_snapshot(RotationStore(args.output_root).load(args.run_id))
    except Exception as exc:
        result = {"status": "FAILED", "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0 if result["status"] == "MATCH" else 1


if __name__ == "__main__":
    raise SystemExit(main())
