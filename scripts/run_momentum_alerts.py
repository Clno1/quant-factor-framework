#!/usr/bin/env python3
"""Run one live momentum scan, optionally delivering an hourly Discord digest."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alerts import (  # noqa: E402
    AlertSettings,
    AlertStateStore,
    DiscordNotifier,
    build_discord_payload,
)
from src.alerts.engine import market_hours_snapshot, run_live_alert_scan  # noqa: E402
from src.alerts.config import load_local_env  # noqa: E402
from src.alerts.state import SIGNAL_RANK  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402
from src.utils.logger import get_logger  # noqa: E402

log = get_logger("run_momentum_alerts")
_NEW_YORK = ZoneInfo("America/New_York")


def _parse_tickers(values: list[str]) -> list[str]:
    return [
        ticker.strip().upper()
        for value in values
        for ticker in value.split(",")
        if ticker.strip()
    ]


def _digest_rows(
    rows: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    pending_map = {str(row["ticker"]): row for row in pending}
    ordered_pending = sorted(pending, key=lambda row: (
        -SIGNAL_RANK.get(str(row.get("signal_type") or "CANDIDATE"), 1),
        -int(row.get("score") or 0),
    ))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in [*ordered_pending, *rows]:
        ticker = str(row.get("ticker") or "")
        if not ticker or ticker in seen:
            continue
        selected.append(pending_map.get(ticker, row))
        seen.add(ticker)
        if len(selected) >= limit:
            break
    delivered_pending = [row for row in selected if str(row.get("ticker")) in pending_map]
    return selected, delivered_pending


def _print_summary(snapshot: dict[str, Any], selected: list[dict[str, Any]]) -> None:
    market = snapshot.get("market_hours") or {}
    print(
        "momentum_alerts "
        f"market_open={bool(market.get('isMarketOpen'))} "
        f"session={snapshot.get('session_date')} quote_time={snapshot.get('quote_time')} "
        f"broad={snapshot.get('broad_count')} strict={snapshot.get('strict_count')} "
        f"pending={snapshot.get('pending_upgrade_count')}"
    )
    for row in selected:
        print(
            f"{row.get('ticker'):>6} {row.get('signal_type'):<20} "
            f"score={int(row.get('score') or 0):>3} "
            f"ret20={float(row.get('return_20d') or 0):>7.2f}% "
            f"adr20={float(row.get('adr_20d') or 0):>6.2f}% "
            f"dv=${float(row.get('dollar_volume') or 0) / 1_000_000:>9.1f}M"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true", help="Deliver the digest to Discord.")
    parser.add_argument("--send-empty", action="store_true", help="Send even when no strict candidate exists.")
    parser.add_argument("--intraday", action="store_true", help="Refresh minute bars for top strict candidates.")
    parser.add_argument(
        "--include-etfs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Include ETFs in this run. Default comes from momentum_alerts.asset_types.include_etfs.",
    )
    parser.add_argument("--market-open-only", action="store_true", help="Skip when FMP says NASDAQ is closed.")
    parser.add_argument(
        "--scheduled-hourly",
        action="store_true",
        help="Run only in the 10:00-15:59 America/New_York hours while the market is open.",
    )
    parser.add_argument("--extra-ticker", action="append", default=[], help="Always monitor ticker(s); comma-separated accepted.")
    parser.add_argument("--max-rows", type=int, default=None)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Safely load KEY=VALUE settings without shell-sourcing the file.",
    )
    args = parser.parse_args()
    if args.env_file is not None:
        if load_local_env(args.env_file) is None:
            raise FileNotFoundError("the requested environment file does not exist")

    settings = AlertSettings.load(
        extra_tickers=_parse_tickers(args.extra_ticker),
        load_env=args.env_file is None,
    )
    if args.include_etfs is not None:
        settings = replace(settings, include_etfs=args.include_etfs)
    store = AlertStateStore(settings.state_path)
    mode = "send" if args.send else "dry-run"
    market_hours = market_hours_snapshot(settings)
    run_id = store.start_run(mode=mode, market_open=bool(market_hours.get("isMarketOpen")))
    try:
        now_et = datetime.now(_NEW_YORK)
        market_required = args.market_open_only or args.scheduled_hourly
        in_hourly_window = 10 <= now_et.hour <= 15
        if market_required and not market_hours.get("isMarketOpen"):
            store.finish_run(run_id, status="skipped_market_closed", delivery_status="not_attempted")
            print(f"skip: {settings.exchange} market is closed")
            return 0
        if args.scheduled_hourly and not in_hourly_window:
            store.finish_run(run_id, status="skipped_outside_hourly_window", delivery_status="not_attempted")
            print(f"skip: outside hourly digest window ({now_et.isoformat(timespec='minutes')})")
            return 0

        snapshot = run_live_alert_scan(
            settings,
            market_hours=market_hours,
            include_intraday=(args.intraday or settings.intraday_enabled),
        )
        pending = store.observe(snapshot["session_date"], snapshot["rows"])
        snapshot["pending_upgrade_count"] = len(pending)
        max_rows = min(20, max(1, args.max_rows or settings.notification_max_rows))
        selected, delivered_pending = _digest_rows(snapshot["rows"], pending, max_rows)
        snapshot["digest_tickers"] = [row["ticker"] for row in selected]

        timestamp = snapshot["generated_at"].replace(":", "").replace("+", "_")
        run_path = settings.runs_dir / f"{timestamp}.json"
        atomic_save_json(snapshot, run_path)
        _print_summary(snapshot, selected)

        delivery_status = "dry_run"
        if args.send:
            should_send = bool(selected) or args.send_empty or settings.send_empty_digest
            if not should_send:
                delivery_status = "skipped_empty"
            else:
                if not settings.discord_configured:
                    raise RuntimeError(
                        "DISCORD_WEBHOOK_URL 未配置。请将它写入项目根目录 .env.local。"
                    )
                mention = any(
                    str(row.get("signal_type") or "").upper() in settings.mention_levels
                    for row in delivered_pending
                )
                payload = build_discord_payload(
                    snapshot,
                    selected,
                    role_id=settings.discord_role_id,
                    mention=mention,
                    dashboard_base_url=settings.dashboard_base_url,
                )
                result = DiscordNotifier(settings.discord_webhook_url).send(payload)
                baseline_candidates = [
                    row for row in snapshot["rows"]
                    if str(row.get("signal_type") or "CANDIDATE").upper() == "CANDIDATE"
                ]
                # A successful hourly digest establishes the ordinary candidate
                # baseline. Higher-priority signals remain pending unless they
                # were actually included in this message.
                store.mark_delivered(
                    snapshot["session_date"],
                    [*baseline_candidates, *delivered_pending],
                )
                delivery_status = f"discord_http_{result.get('status')}"
                print(f"discord delivery: {delivery_status}")
        store.finish_run(
            run_id,
            status="completed",
            snapshot=snapshot,
            delivery_status=delivery_status,
        )
        print(f"snapshot: {run_path}")
        return 0
    except Exception as exc:  # noqa: BLE001
        log.exception("Momentum alert run failed")
        store.finish_run(run_id, status="failed", delivery_status="failed", error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
