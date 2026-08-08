#!/usr/bin/env python
"""Process queued custom-universe requests through the sole FMP writer."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--env-file",
        default=None,
        help="KEY=VALUE file (default: project .env.local)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    from src.data.request_worker import process_pending_data_requests
    from src.storage import DATA_REQUEST_FAILED
    from src.utils.env import load_local_env

    load_local_env(args.env_file)
    results = process_pending_data_requests(limit=max(1, int(args.limit)))
    for result in results:
        print(
            f"request={result.request_id} status={result.status} "
            f"details={result.payload}"
        )
    return 1 if any(r.status == DATA_REQUEST_FAILED for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
