"""Reconcile paper ledgers into Discord fill events and daily summaries."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from src.alerts.discord import (
    DiscordDeliveryError,
    DiscordNotifier,
    validate_discord_payload,
)
from src.config import PROJECT_ROOT
from src.operations.evidence import safe_text
from src.papertrading.notification_state import (
    KIND_DAILY_SUMMARY,
    KIND_FILL,
    PaperNotificationState,
)
from src.papertrading.store import list_accounts, load_account, load_table
from src.utils.market_calendar import latest_completed_xnys_session


DEFAULT_STATE_PATH = PROJECT_ROOT / "outputs" / "paper_notifications" / "state.sqlite3"


def _strict_bool(value: Any, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _positive_int(value: Any, *, default: int, label: str) -> int:
    try:
        parsed = int(value if value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class PaperNotificationSettings:
    state_path: Path
    webhook_url: str
    delivery_enabled: bool
    dashboard_base_url: str
    max_attempts: int
    batch_limit: int

    @property
    def discord_configured(self) -> bool:
        return bool(self.webhook_url)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "PaperNotificationSettings":
        values = os.environ if env is None else env
        raw_path = str(values.get("PAPER_NOTIFICATION_STATE_PATH") or "").strip()
        state_path = Path(raw_path).expanduser() if raw_path else DEFAULT_STATE_PATH
        if not state_path.is_absolute():
            state_path = PROJECT_ROOT / state_path
        settings = cls(
            state_path=state_path,
            webhook_url=str(
                values.get("PAPER_DISCORD_WEBHOOK_URL") or ""
            ).strip(),
            delivery_enabled=_strict_bool(
                values.get("PAPER_DISCORD_DELIVERY_ENABLED"),
                default=False,
            ),
            dashboard_base_url=str(
                values.get("PAPER_DASHBOARD_BASE_URL") or ""
            ).strip().rstrip("/"),
            max_attempts=_positive_int(
                values.get("PAPER_DISCORD_MAX_ATTEMPTS"),
                default=3,
                label="PAPER_DISCORD_MAX_ATTEMPTS",
            ),
            batch_limit=_positive_int(
                values.get("PAPER_DISCORD_BATCH_LIMIT"),
                default=25,
                label="PAPER_DISCORD_BATCH_LIMIT",
            ),
        )
        if settings.delivery_enabled and not settings.webhook_url:
            raise ValueError(
                "PAPER_DISCORD_WEBHOOK_URL is required when delivery is enabled"
            )
        if settings.webhook_url:
            DiscordNotifier(settings.webhook_url, max_rate_limit_retries=0)
        return settings


def _number(value: Any, *, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _money(value: Any) -> str:
    return f"${_number(value):,.2f}"


def _signed_money(value: Any) -> str:
    return f"{_number(value):+,.2f} 美元"


def _short(value: Any, length: int = 12) -> str:
    text = str(value or "")
    return text[:length] if text else "无"


def _safe_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("Asia/Singapore")
    return timestamp.tz_convert("UTC").isoformat()


def _account_url(base_url: str, account_id: str) -> str:
    return f"{base_url}/paper/{account_id}" if base_url else ""


def _numeric_column(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame.columns:
        return pd.Series(0.0, index=frame.index, dtype=float)
    return pd.to_numeric(frame[name], errors="coerce").fillna(0.0)


def build_fill_discord_payload(
    *,
    account: dict[str, Any],
    fill: dict[str, Any],
    dataset_version_id: str | None,
    dashboard_base_url: str = "",
) -> dict[str, Any]:
    side = str(fill.get("side") or "").upper()
    side_zh = "买入" if side == "BUY" else "卖出"
    ticker = str(fill.get("ticker") or "未知")
    quantity = int(_number(fill.get("quantity")))
    raw_open = _number(fill.get("raw_open_price"))
    fill_price = _number(fill.get("fill_price"))
    fee = _number(fill.get("fee"))
    slippage_cost = _number(fill.get("slippage_cost"))
    regulatory = sum(
        _number(fill.get(name))
        for name in ("sec_fee", "finra_taf", "finra_cat")
    )
    account_id = str(account.get("id") or fill.get("account_id") or "")
    fields = [
        {
            "name": "成交",
            "value": (
                f"**{side_zh} {ticker} {quantity} 股**\n"
                f"成交额 {_money(fill.get('notional'))}"
            ),
            "inline": True,
        },
        {
            "name": "价格与滑点",
            "value": (
                f"原始开盘价 {_money(raw_open)}\n"
                f"模拟成交价 {_money(fill_price)}\n"
                f"滑点 {_number(fill.get('slippage_bps')):.3f} bps / "
                f"{_money(slippage_cost)}"
            ),
            "inline": True,
        },
        {
            "name": "交易费用",
            "value": (
                f"总手续费 {_money(fee)}\n"
                f"券商佣金 {_money(fill.get('broker_commission'))}\n"
                f"监管费用 {_money(regulatory)}"
            ),
            "inline": True,
        },
        {
            "name": "可审计合同",
            "value": (
                f"决策日 `{fill.get('decision_date')}` → 成交日 "
                f"`{fill.get('fill_date')}`\n"
                f"成交 `next_open` · 费用 `{fill.get('fee_model')}` · "
                f"滑点 `{fill.get('slippage_model')}`\n"
                f"行情版本 `{_short(dataset_version_id)}` · "
                f"订单 `{_short(fill.get('order_id'), 8)}`"
            ),
            "inline": False,
        },
    ]
    url = _account_url(dashboard_base_url, account_id)
    if url:
        fields.append(
            {
                "name": "账户页面",
                "value": f"[查看模拟盘账户]({url})",
                "inline": False,
            }
        )
    embed: dict[str, Any] = {
        "title": f"模拟盘成交 · {side_zh} {ticker}",
        "description": (
            f"账户 **{account.get('name') or account_id}** 产生一笔新的模拟成交。"
        ),
        "color": 0x16A34A if side == "BUY" else 0xDC2626,
        "fields": fields,
        "footer": {"text": "内部模拟交易，不是真实券商订单"},
    }
    timestamp = _safe_timestamp(fill.get("filled_at"))
    if timestamp:
        embed["timestamp"] = timestamp
    payload = {
        "username": "Paper Trading",
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [embed],
    }
    return validate_discord_payload(payload)


def _latest_run(account_id: str, *, session: str | None = None) -> dict[str, Any]:
    runs = load_table(account_id, "runs")
    if runs.empty:
        return {}
    if session and "mark_date" in runs.columns:
        matching = runs[runs["mark_date"].astype(str) == session]
        if not matching.empty:
            runs = matching
    if "run_at" in runs.columns:
        runs = runs.sort_values("run_at", kind="stable")
    return runs.iloc[-1].to_dict()


def _daily_account_field(
    account: dict[str, Any],
    *,
    target_session: str,
    dashboard_base_url: str,
) -> tuple[dict[str, Any], dict[str, float | int | bool]]:
    account_id = str(account.get("id") or "")
    curve = load_table(account_id, "equity_curve")
    current_equity = _number(account.get("last_equity"))
    previous_equity = current_equity
    if not curve.empty and "date" in curve.columns:
        curve = curve.copy()
        curve["_date"] = pd.to_datetime(curve["date"], errors="coerce")
        curve = curve[curve["_date"].notna()].sort_values("_date", kind="stable")
        eligible = curve[curve["_date"] <= pd.Timestamp(target_session)]
        if not eligible.empty:
            current_equity = _number(eligible.iloc[-1].get("equity"), default=current_equity)
            if len(eligible) >= 2:
                previous_equity = _number(
                    eligible.iloc[-2].get("equity"),
                    default=current_equity,
                )
    daily_pnl = current_equity - previous_equity
    initial_cash = _number(account.get("initial_cash"))
    cumulative_pnl = current_equity - initial_cash
    fills = load_table(account_id, "fills")
    fills_today = pd.DataFrame()
    if not fills.empty and "fill_date" in fills.columns:
        fills_today = fills[fills["fill_date"].astype(str) == target_session]
    buy_notional = 0.0
    sell_notional = 0.0
    fees = 0.0
    slippage = 0.0
    if not fills_today.empty:
        side = fills_today.get("side", pd.Series(dtype=str)).astype(str)
        notionals = _numeric_column(fills_today, "notional")
        buy_notional = float(notionals[side.eq("BUY")].sum())
        sell_notional = float(notionals[side.eq("SELL")].sum())
        fees = float(
            _numeric_column(fills_today, "fee").sum()
        )
        slippage = float(
            _numeric_column(fills_today, "slippage_cost").sum()
        )
    orders = load_table(account_id, "orders")
    pending = 0
    if not orders.empty and "status" in orders.columns:
        pending = int(orders["status"].astype(str).eq("pending").sum())
    positions = load_table(account_id, "positions")
    mark_date = str(account.get("last_mark_date") or "")
    error = str(account.get("last_error") or "")
    current = mark_date == target_session and not error
    status_text = "正常" if current else ("失败" if error else "数据未更新")
    run = _latest_run(account_id, session=target_session)
    lines = [
        f"状态 **{status_text}** · 净值 `{_money(current_equity)}` · "
        f"当日 `{_signed_money(daily_pnl)}`",
        f"累计 `{_signed_money(cumulative_pnl)}` · 现金 `{_money(account.get('cash'))}` · "
        f"持仓 `{len(positions)}` 只",
        f"成交 `{len(fills_today)}` 笔（买入 {_money(buy_notional)} / "
        f"卖出 {_money(sell_notional)}）· 待执行 `{pending}` 笔",
        f"手续费 `{_money(fees)}` · 滑点成本 `{_money(slippage)}` · "
        f"行情版本 `{_short(run.get('dataset_version_id'))}`",
    ]
    if error:
        lines.append(f"错误：`{safe_text(error)[:300]}`")
    url = _account_url(dashboard_base_url, account_id)
    if url:
        lines.append(f"[查看账户]({url})")
    field = {
        "name": str(account.get("name") or account_id)[:256],
        "value": "\n".join(lines)[:1024],
        "inline": False,
    }
    metrics: dict[str, float | int | bool] = {
        "equity": current_equity,
        "daily_pnl": daily_pnl,
        "fills": len(fills_today),
        "fees": fees,
        "slippage": slippage,
        "current": current,
    }
    return field, metrics


def build_daily_discord_payload(
    *,
    accounts: list[dict[str, Any]],
    target_session: str,
    dashboard_base_url: str = "",
    generated_at: str | None = None,
) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    metrics: list[dict[str, float | int | bool]] = []
    for account in accounts:
        field, account_metrics = _daily_account_field(
            account,
            target_session=target_session,
            dashboard_base_url=dashboard_base_url,
        )
        if len(fields) < 20:
            fields.append(field)
        metrics.append(account_metrics)
    total_equity = sum(_number(row.get("equity")) for row in metrics)
    total_daily_pnl = sum(_number(row.get("daily_pnl")) for row in metrics)
    total_fills = sum(int(row.get("fills") or 0) for row in metrics)
    total_fees = sum(_number(row.get("fees")) for row in metrics)
    total_slippage = sum(_number(row.get("slippage")) for row in metrics)
    current_count = sum(bool(row.get("current")) for row in metrics)
    if not fields:
        fields = [
            {
                "name": "没有启用账户",
                "value": "当前没有 active 模拟盘账户。",
                "inline": False,
            }
        ]
    fixed_characters = len(f"模拟盘日结 · {target_session}") + 300
    while (
        len(fields) > 1
        and fixed_characters
        + sum(len(field["name"]) + len(field["value"]) for field in fields)
        > 5_400
    ):
        fields.pop()
    omitted = max(0, len(accounts) - len(fields))
    if omitted:
        fields.append({
            "name": "其余账户",
            "value": f"另有 {omitted} 个账户已计入汇总；请在主站查看逐账户明细。",
            "inline": False,
        })
    description = (
        f"目标交易日 `{target_session}` · 账户 **{current_count}/{len(accounts)}** 已更新\n"
        f"总权益 **{_money(total_equity)}** · 当日盈亏 **{_signed_money(total_daily_pnl)}** · "
        f"成交 **{total_fills}** 笔\n"
        f"手续费 {_money(total_fees)} · 滑点成本 {_money(total_slippage)}"
    )
    all_current = current_count == len(accounts) and bool(accounts)
    payload = {
        "username": "Paper Trading",
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [
            {
                "title": f"模拟盘日结 · {target_session}",
                "description": description,
                "color": 0x2563EB if all_current else 0xF59E0B,
                "fields": fields,
                "footer": {"text": "内部模拟交易，不是真实券商账户"},
                "timestamp": generated_at or datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    return validate_discord_payload(payload)


class PaperNotificationService:
    def __init__(
        self,
        settings: PaperNotificationSettings,
        *,
        state: PaperNotificationState | None = None,
        notifier: DiscordNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.state = state or PaperNotificationState(settings.state_path)
        self.notifier = notifier
        if self.notifier is None and settings.delivery_enabled:
            self.notifier = DiscordNotifier(settings.webhook_url)

    def reconcile_fills(self, *, baseline: bool = False) -> int:
        staged = 0
        for summary in list_accounts():
            account_id = str(summary.get("id") or "")
            account = load_account(account_id)
            if not account:
                continue
            fills = load_table(account_id, "fills")
            if fills.empty:
                continue
            for fill in fills.to_dict(orient="records"):
                fill_id = str(fill.get("fill_id") or "")
                fill_date = str(fill.get("fill_date") or "")
                if not fill_id or not fill_date:
                    raise ValueError(
                        f"Paper fill has no stable identity/session: account={account_id}"
                    )
                run = _latest_run(account_id, session=fill_date)
                payload = build_fill_discord_payload(
                    account=account,
                    fill=fill,
                    dataset_version_id=(
                        str(run.get("dataset_version_id"))
                        if run.get("dataset_version_id")
                        else None
                    ),
                    dashboard_base_url=self.settings.dashboard_base_url,
                )
                created = self.state.stage(
                    delivery_id=f"paper-fill:{fill_id}",
                    kind=KIND_FILL,
                    account_id=account_id,
                    target_session=fill_date,
                    source_id=fill_id,
                    payload=payload,
                    baseline=baseline,
                )
                staged += int(created)
        return staged

    def stage_daily_summary(self, *, target_session: str | None = None) -> bool:
        session = target_session or latest_completed_xnys_session().strftime(
            "%Y-%m-%d"
        )
        accounts = []
        for summary in list_accounts():
            if str(summary.get("status") or "").lower() != "active":
                continue
            account = load_account(str(summary.get("id") or ""))
            if account:
                accounts.append(account)
        payload = build_daily_discord_payload(
            accounts=accounts,
            target_session=session,
            dashboard_base_url=self.settings.dashboard_base_url,
        )
        return self.state.stage(
            delivery_id=f"paper-daily:{session}",
            kind=KIND_DAILY_SUMMARY,
            account_id=None,
            target_session=session,
            source_id=session,
            payload=payload,
        )

    def drain(self, *, kinds: set[str]) -> dict[str, int]:
        results = {"sent": 0, "failed": 0, "unknown": 0}
        if not self.settings.delivery_enabled or self.notifier is None:
            return results
        for _ in range(self.settings.batch_limit):
            claim = self.state.claim_next(
                kinds=kinds,
                max_attempts=self.settings.max_attempts,
            )
            if claim is None:
                break
            try:
                result = self.notifier.send(claim.payload)
            except DiscordDeliveryError as exc:
                self.state.mark_failed(
                    claim,
                    error_code=exc.reason,
                    error_message=str(exc),
                    uncertain=exc.uncertain,
                    retryable=exc.retryable,
                )
                results["unknown" if exc.uncertain else "failed"] += 1
                break
            self.state.mark_sent(
                claim,
                message_id=str(result["message_id"]),
            )
            results["sent"] += 1
        return results


__all__ = [
    "DEFAULT_STATE_PATH",
    "PaperNotificationService",
    "PaperNotificationSettings",
    "build_daily_discord_payload",
    "build_fill_discord_payload",
]
