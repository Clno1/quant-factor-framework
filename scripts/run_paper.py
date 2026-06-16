#!/usr/bin/env python
"""Run internal paper trading accounts.

Examples:
    python scripts/run_paper.py
    python scripts/run_paper.py --account-id <UUID>
    python scripts/run_paper.py --asof 2026-06-12
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.papertrading.definition import STATUS_ACTIVE  # noqa: E402
from src.papertrading.runner import run_account_once  # noqa: E402
from src.papertrading.store import list_accounts  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

log = get_logger("run_paper")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run internal paper trading accounts.")
    p.add_argument("--account-id", default=None, help="只运行指定模拟盘账户")
    p.add_argument("--asof", default=None, help="按指定 YYYY-MM-DD 截止日期运行")
    p.add_argument(
        "--include-paused",
        action="store_true",
        help="运行全部账户，包括 paused；默认只跑 active",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    accounts = list_accounts()
    if args.account_id:
        accounts = [a for a in accounts if a.get("id") == args.account_id]
    elif not args.include_paused:
        accounts = [a for a in accounts if a.get("status") == STATUS_ACTIVE]

    if not accounts:
        log.info("No paper accounts to run.")
        return 0

    ok = 0
    failed = 0
    for account in accounts:
        aid = str(account.get("id") or "")
        if not aid:
            continue
        try:
            result = run_account_once(aid, asof=args.asof)
            run = result.get("run") or {}
            log.info(
                "[%s] done: decision=%s mark=%s equity=%.2f fills=%s new_orders=%s pending=%s",
                aid,
                run.get("decision_date"),
                run.get("mark_date"),
                float(run.get("equity") or 0.0),
                run.get("fills_count"),
                run.get("orders_created"),
                run.get("pending_orders"),
            )
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.exception("[%s] failed: %s", aid, e)
    log.info("Paper run finished: ok=%d failed=%d", ok, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
