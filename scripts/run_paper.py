#!/usr/bin/env python
"""Run internal paper trading accounts.

Examples:
    python scripts/run_paper.py
    python scripts/run_paper.py --account-id <UUID>
    python scripts/run_paper.py --asof 2026-06-12
    python scripts/run_paper.py --refresh-watchlists
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.papertrading.definition import STATUS_ACTIVE  # noqa: E402
from src.papertrading.runner import run_account_once  # noqa: E402
from src.papertrading.store import list_accounts, load_account  # noqa: E402
from src.data.access import (  # noqa: E402
    enqueue_market_data_request,
    watchlist_universe_frame,
)
from src.data.request_worker import process_pending_data_requests  # noqa: E402
from src.data.universe_ids import watchlist_snapshot_data_universe  # noqa: E402
from src.config import CONFIG  # noqa: E402
from src.storage import DATA_REQUEST_SUCCESS, app_database  # noqa: E402
from src.utils.date_utils import resolve_date_range  # noqa: E402
from src.utils.identifiers import InvalidResourceId, canonical_uuid  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

log = get_logger("run_paper")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run internal paper trading accounts.")
    p.add_argument("--account-id", default=None, help="只运行指定模拟盘账户")
    p.add_argument("--asof", default=None, help="按指定 YYYY-MM-DD 截止日期运行")
    p.add_argument(
        "--refresh-watchlists",
        action="store_true",
        help="运行前强制刷新 active 账户自定义 Watchlist 的 OHLCV",
    )
    return p.parse_args()


def _refresh_watchlist_ohlcv(accounts: list[dict]) -> None:
    """Queue and synchronously publish custom universes through MarketDataWriter."""
    request_ids: list[str] = []
    start_iso, end_iso, _ = resolve_date_range(
        CONFIG.date_range.start,
        CONFIG.date_range.end,
    )
    for summary in accounts:
        account = load_account(str(summary.get("id") or ""))
        snapshot = (account or {}).get("watchlist_snapshot") or {}
        if not snapshot:
            continue
        request = enqueue_market_data_request(
            data_universe=watchlist_snapshot_data_universe(snapshot),
            universe_frame=watchlist_universe_frame(snapshot),
            start=start_iso,
            end=end_iso,
            initial_start=(
                pd.Timestamp(start_iso) - pd.Timedelta(days=400)
            ).strftime("%Y-%m-%d"),
            consumer_kind="paper_account",
            consumer_id=str(account["id"]),
            force=True,
        )
        request_ids.append(request.request_id)
    if not request_ids:
        return
    log.info(
        "Processing %d paper custom-universe data requests",
        len(request_ids),
    )
    process_pending_data_requests(limit=max(len(request_ids), 10))
    database = app_database()
    not_ready = []
    for request_id in request_ids:
        request = database.get_data_request(request_id)
        if request is None or request.status != DATA_REQUEST_SUCCESS:
            not_ready.append(
                {
                    "request_id": request_id,
                    "status": request.status if request is not None else "missing",
                }
            )
    if not_ready:
        raise RuntimeError(
            "Paper custom-universe publication is not ready: "
            f"{not_ready}"
        )


def main() -> int:
    args = parse_args()
    accounts = list_accounts()
    if args.account_id:
        try:
            requested_id = canonical_uuid(
                args.account_id,
                label="account_id",
            )
        except InvalidResourceId as exc:
            log.error("%s", exc)
            return 2
        accounts = [a for a in accounts if a.get("id") == requested_id]
        if not accounts:
            log.error("Paper account not found: %s", requested_id)
            return 1
    else:
        accounts = [a for a in accounts if a.get("status") == STATUS_ACTIVE]

    if not accounts:
        log.info("No paper accounts to run.")
        return 0
    if args.refresh_watchlists:
        _refresh_watchlist_ohlcv(accounts)

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
