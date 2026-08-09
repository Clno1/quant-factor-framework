"""Discord payload contract and transport protocol for minute signals."""
from __future__ import annotations

from typing import Any, Protocol
from urllib.parse import quote

from src.breakouts.live.models import BreakoutSignal


class SignalNotifier(Protocol):
    def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


def _price(value: float | None) -> str:
    return "n/a" if value is None else f"${float(value):.2f}"


def _multiple(value: float | None) -> str:
    return "n/a" if value is None else f"{float(value):.2f}x"


def _money(value: float) -> str:
    if abs(value) >= 1_000_000_000:
        return f"${value / 1_000_000_000:.2f}B"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    return f"${value:,.0f}"


def build_signal_discord_payload(
    signal: BreakoutSignal,
    *,
    role_id: str = "",
    dashboard_base_url: str = "",
) -> dict[str, Any]:
    """Build one bounded Discord message for one exact minute-bar signal."""
    opening_range = (
        "n/a"
        if signal.opening_range_minutes is None
        else f"OR{signal.opening_range_minutes} {_price(signal.opening_range_high)}"
    )
    description = (
        f"`{signal.ticker}` 已由完整 1 分钟 K 线确认 **{signal.signal_type}**。\n"
        f"触发价 **{_price(signal.price)}** · 突破位 **{_price(signal.breakout_level)}** · "
        f"{opening_range}"
    )
    fields = [
        {
            "name": "日线强度",
            "value": (
                f"Setup {signal.setup_score} 分 · 20D {signal.return20_live:+.1f}% · "
                f"ADR20 {signal.adr20_live:.1f}% · 当日额 {_money(signal.dollar_volume)}"
            ),
            "inline": False,
        },
        {
            "name": "盘中确认",
            "value": (
                f"VWAP {_price(signal.vwap)} · RVOL {_multiple(signal.relative_volume)} · "
                f"MA10 {_price(signal.ma10)} · MA20 {_price(signal.ma20)} · "
                f"MA50 {_price(signal.ma50)}"
            ),
            "inline": False,
        },
        {
            "name": "审计信息",
            "value": (
                f"Bar `{signal.bar_timestamp.isoformat(timespec='minutes')}` · "
                f"算法 `{signal.algorithm_version}` · 参数 `{signal.parameter_version}`\n"
                f"原因：{', '.join(signal.reasons)}"
            )[:1024],
            "inline": False,
        },
    ]
    if dashboard_base_url:
        fields.append({
            "name": "诊断",
            "value": (
                f"[打开 {signal.ticker} 分钟诊断]"
                f"({dashboard_base_url}/breakouts/{quote(signal.ticker)}?universe=US_ACTIVE)"
            ),
            "inline": False,
        })
    payload: dict[str, Any] = {
        "username": "Momentum Alerts",
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "动量突破 · 分钟确认",
            "description": description,
            "color": 0x00C853,
            "fields": fields,
            "footer": {"text": "分钟信号已去重并经过冷却检查；研究提醒，不构成交易建议"},
            "timestamp": signal.triggered_at.isoformat(timespec="seconds"),
        }],
    }
    if role_id.isascii() and role_id.isdigit():
        payload["content"] = f"<@&{role_id}> 新的分钟级动量突破信号"
        payload["allowed_mentions"] = {"parse": [], "roles": [role_id]}
    return payload


__all__ = ["SignalNotifier", "build_signal_discord_payload"]
