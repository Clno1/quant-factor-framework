"""Discord incoming-webhook delivery for momentum scan digests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlparse

import requests


class DiscordDeliveryError(RuntimeError):
    """Raised when a Discord webhook cannot accept a message."""


def _money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if abs(number) >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if abs(number) >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    return f"${number:,.0f}"


def _pct(value: Any, *, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return f"{number:+.1f}%" if signed else f"{number:.1f}%"


def _field(row: dict[str, Any], dashboard_base_url: str = "") -> dict[str, Any]:
    ticker = str(row.get("ticker") or "")
    signal = str(row.get("signal_type") or "CANDIDATE")
    score = int(row.get("score") or 0)
    name = str(row.get("name") or "")[:80]
    title = f"{ticker} · {signal} · {score}分"
    if row.get("is_upgrade"):
        title = "NEW · " + title
    pivot = row.get("pivot")
    pivot_text = "n/a" if pivot is None else f"${float(pivot):.2f}"
    lines = [
        name or "Unnamed security",
        f"现价 ${float(row.get('close') or 0):.2f} · Pivot {pivot_text} · 距离 {_pct(row.get('pivot_distance'), signed=True)}",
        f"20D {_pct(row.get('return_20d'), signed=True)} · ADR20 {_pct(row.get('adr_20d'))}",
        f"当日额 {_money(row.get('dollar_volume'))} · 20日均额 {_money(row.get('avg_dollar_volume_20d'))}",
    ]
    if row.get("intraday_trigger"):
        lines.append(str(row["intraday_trigger"]))
    if dashboard_base_url:
        lines.append(f"[打开诊断]({dashboard_base_url}/breakouts/{ticker}?universe=US_ACTIVE)")
    return {"name": title[:256], "value": "\n".join(lines)[:1024], "inline": False}


def build_discord_payload(
    snapshot: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    *,
    role_id: str = "",
    mention: bool = False,
    dashboard_base_url: str = "",
) -> dict[str, Any]:
    selected = list(rows)
    market = snapshot.get("market_hours") or {}
    is_open = bool(market.get("isMarketOpen"))
    status = "OPEN" if is_open else "CLOSED / SHADOW"
    pending = int(snapshot.get("pending_upgrade_count") or 0)
    asset_scope = "股票 + ETF" if snapshot.get("include_etfs") else "仅股票"
    description = (
        f"市场 **{status}** · 范围 **{asset_scope}** · 宽筛 **{snapshot.get('broad_count', 0)}** · "
        f"实时硬筛 **{snapshot.get('strict_count', 0)}** · 新增/升级 **{pending}**\n"
        f"报价时间 `{snapshot.get('quote_time') or 'n/a'}`"
    )
    fields = [_field(row, dashboard_base_url) for row in selected[:20]]
    if not fields:
        fields = [{"name": "本轮没有严格候选", "value": "未发现满足当前四项硬筛的标的。", "inline": False}]
    signal_types = {str(row.get("signal_type") or "") for row in selected}
    if "OPENING_RANGE_BREAK" in signal_types or "BREAKOUT" in signal_types:
        color = 0x00C853
    elif "READY" in signal_types:
        color = 0xFFB300
    else:
        color = 0x42A5F5 if is_open else 0x6B7280
    payload: dict[str, Any] = {
        "username": "Momentum Alerts",
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "动量交易 · 小时扫描",
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {"text": "研究提醒，不构成交易建议"},
            "timestamp": snapshot.get("generated_at"),
        }],
    }
    if mention and role_id.isdigit():
        payload["content"] = f"<@&{role_id}> 发现新的高优先级动量信号"
        payload["allowed_mentions"] = {"parse": [], "roles": [role_id]}
    return payload


@dataclass
class DiscordNotifier:
    webhook_url: str
    timeout: float = 15.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.webhook_url)
        allowed_hosts = {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
        if parsed.scheme != "https" or parsed.hostname not in allowed_hosts \
                or "/api/webhooks/" not in parsed.path:
            raise ValueError("DISCORD_WEBHOOK_URL 不是有效的 Discord Incoming Webhook")

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        separator = "&" if "?" in self.webhook_url else "?"
        response = requests.post(
            self.webhook_url + separator + "wait=true",
            json=payload,
            timeout=self.timeout,
        )
        if response.status_code not in {200, 204}:
            body = response.text[:300].replace("\n", " ")
            raise DiscordDeliveryError(
                f"Discord webhook returned HTTP {response.status_code}: {body}"
            )
        if response.status_code == 204 or not response.content:
            return {"status": response.status_code}
        data = response.json()
        return {"status": response.status_code, "message_id": data.get("id")}
