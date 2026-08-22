#!/usr/bin/env python3
"""Run the independent read-only operations website."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.operations.registry import OperationsRegistry  # noqa: E402
from src.operations_web.security import validate_operations_exposure  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/operations.yaml")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    registry = OperationsRegistry(args.config)
    host = args.host or registry.settings.web_host
    port = args.port or registry.settings.web_port
    validate_operations_exposure(host)
    import uvicorn
    from src.operations_web.app import create_app

    uvicorn.run(
        create_app(registry=registry),
        host=host,
        port=port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
