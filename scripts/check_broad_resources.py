#!/usr/bin/env python3
"""Refuse broad background work when the fixed SG host lacks safe headroom."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minimum-memory-mb", type=float, default=350.0)
    parser.add_argument("--minimum-disk-gb", type=float, default=15.0)
    parser.add_argument("--path", default=str(ROOT))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _available_memory_mb() -> float:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        values: dict[str, int] = {}
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            token = raw.strip().split()[0]
            if token.isdigit():
                values[key] = int(token)
        if "MemAvailable" in values:
            return values["MemAvailable"] / 1024.0
    # macOS does not expose SC_AVPHYS_PAGES.  Production runs on Linux and
    # therefore uses MemAvailable above; the total-page fallback keeps local
    # validation portable without introducing another runtime dependency.
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
    except (OSError, ValueError):
        pages = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    return float(pages * page_size) / (1024.0 * 1024.0)


def check_resources(
    *,
    path: str | Path,
    minimum_memory_mb: float,
    minimum_disk_gb: float,
) -> dict:
    available_memory_mb = _available_memory_mb()
    available_disk_gb = shutil.disk_usage(Path(path)).free / (1024.0 ** 3)
    checks = {
        "memory": {
            "passed": available_memory_mb >= float(minimum_memory_mb),
            "observed_mb": round(available_memory_mb, 3),
            "minimum_mb": float(minimum_memory_mb),
        },
        "disk": {
            "passed": available_disk_gb >= float(minimum_disk_gb),
            "observed_gb": round(available_disk_gb, 3),
            "minimum_gb": float(minimum_disk_gb),
        },
    }
    return {
        "status": (
            "PASS" if all(item["passed"] for item in checks.values()) else "BLOCKED"
        ),
        "path": str(Path(path).resolve()),
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = check_resources(
        path=args.path,
        minimum_memory_mb=args.minimum_memory_mb,
        minimum_disk_gb=args.minimum_disk_gb,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        memory = report["checks"]["memory"]
        disk = report["checks"]["disk"]
        print(
            f"broad resources: {report['status']} "
            f"memory={memory['observed_mb']:.1f}MB "
            f"disk={disk['observed_gb']:.1f}GB"
        )
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
