"""Discord payloads dedicated to the two premarket report contracts."""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from src.alerts.discord import is_discord_snowflake, validate_discord_payload

from .models import DigestChannel, PremarketContext
from .settings import PremarketDigestSettings


def _safe_text(value: Any, limit: int) -> str:
    if value is None or value is pd.NA:
        text = ""
    else:
        try:
            missing = bool(pd.isna(value))
        except (TypeError, ValueError):
            missing = False
        text = "" if missing else str(value)
    text = text.replace("@", "@\u200b").replace("\x00", "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _embed_text_length(embed: dict[str, Any]) -> int:
    total = len(str(embed.get("title") or "")) + len(str(embed.get("description") or ""))
    for field in embed.get("fields") or []:
        total += len(str(field.get("name") or "")) + len(str(field.get("value") or ""))
    total += len(str((embed.get("footer") or {}).get("text") or ""))
    total += len(str((embed.get("author") or {}).get("name") or ""))
    return total


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct_points(value: Any, *, digits: int = 1) -> str:
    number = _number(value)
    return "n/a" if number is None else f"{number:+.{digits}f}%"


def _pct_decimal(value: Any, *, digits: int = 2, signed: bool = True) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    format_spec = f"+.{digits}f" if signed else f".{digits}f"
    return format(number * 100.0, format_spec) + "%"


def _money(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "n/a"
    if abs(number) >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if abs(number) >= 1_000_000:
        return f"${number / 1_000_000:.1f}M"
    return f"${number:,.0f}"


def _mentions(role_id: str, message: str, *, enabled: bool) -> tuple[str, dict[str, Any]]:
    if enabled and is_discord_snowflake(role_id):
        return f"<@&{role_id}> {message}", {"parse": [], "roles": [role_id]}
    return "", {"parse": []}


def build_momentum_payload(
    report: dict[str, Any],
    context: PremarketContext,
    settings: PremarketDigestSettings,
) -> dict[str, Any]:
    rows = list(report.get("rows") or [])[: settings.momentum_max_rows]
    high_priority = any(
        _safe_text(row.get("status"), 32).upper() in {"READY", "BREAKOUT"}
        for row in rows
    )
    content, allowed_mentions = _mentions(
        settings.role_for(DigestChannel.MOMENTUM),
        "美股盘前动量摘要已更新",
        enabled=high_priority,
    )
    raw_regime = report.get("market_regime")
    regime = raw_regime if isinstance(raw_regime, dict) else {}
    regime_status = _safe_text(regime.get("status"), 32) or "UNKNOWN"
    asset_scope = _safe_text(report.get("asset_scope"), 32)
    coverage = _number(report.get("exact_asof_coverage"))
    coverage_text = "n/a" if coverage is None else f"{coverage:.1%}"
    evaluable_coverage = _number(report.get("evaluable_history_coverage"))
    evaluable_coverage_text = (
        "n/a" if evaluable_coverage is None else f"{evaluable_coverage:.1%}"
    )
    description = (
        f"开盘日 **{context.target_session}** · 数据截至 **{context.source_session} EOD**\n"
        f"范围 **{_safe_text(report.get('universe'), 32)} / {'股票+ETF' if asset_scope == 'stocks_and_etfs' else '仅股票'}** · "
        f"精确日线覆盖 **{report.get('exact_asof_count', 0)}/{report.get('universe_count', 0)} ({coverage_text})**\n"
        f"可计算历史覆盖 **{report.get('evaluable_history_count', 0)}/{report.get('universe_count', 0)} ({evaluable_coverage_text})** · "
        f"QQQ 市场过滤 **{regime_status}** · 候选 **{report.get('candidate_count', 0)}** "
        f"(BREAKOUT {report.get('breakout_count', 0)} / READY {report.get('ready_count', 0)} / "
        f"SETUP {report.get('setup_count', 0)} / FORMING {report.get('forming_count', 0)})"
    )
    fields: list[dict[str, Any]] = []
    footer_text = (
        f"delivery momentum:{context.target_session} · 截至上一完整交易日收盘 · "
        "研究提醒，不构成交易建议"
    )
    embed = {
        "title": f"美股盘前动量摘要 · {context.target_session}",
        "description": description,
        "color": 0x00C853 if high_priority else 0x42A5F5,
        "fields": fields,
        "footer": {"text": footer_text},
        "timestamp": context.generated_at,
    }
    for row in rows:
        ticker = _safe_text(row.get("ticker"), 24)
        status = _safe_text(row.get("status"), 32) or "FORMING"
        score = int(_number(row.get("score")) or 0)
        name = _safe_text(row.get("name"), 90) or "Unnamed security"
        close = _number(row.get("close"))
        pivot = _number(row.get("pivot"))
        close_text = "n/a" if close is None else f"${close:.2f}"
        pivot_text = "n/a" if pivot is None else f"${pivot:.2f}"
        lines = [
            name,
            f"收盘 {close_text} · Pivot {pivot_text} · 距离 {_pct_points(row.get('pivot_distance'))}",
            f"20D {_pct_points(row.get('return_20d'))} · ADR20 {_pct_points(row.get('adr_20d'), digits=1).lstrip('+')}",
            f"当日额 {_money(row.get('dollar_volume'))} · 20日均额 {_money(row.get('avg_dollar_volume_20d'))}",
        ]
        if settings.dashboard_base_url:
            lines.append(
                f"[打开诊断]({settings.dashboard_base_url}/breakouts/{ticker}?universe={settings.momentum_universe})"
            )
        field = {
            "name": _safe_text(f"{ticker} · {status} · {score}分", 256),
            "value": _safe_text("\n".join(lines), 1024),
            "inline": False,
        }
        fields.append(field)
        if _embed_text_length(embed) > 5_500:
            fields.pop()
            break
    if not fields:
        fields.append(
            {
                "name": "本次没有严格候选",
                "value": "T-1 完整日线中没有同时通过 20D、ADR20、当日额和20日均额硬门槛的标的。",
                "inline": False,
            }
        )
    payload = {
        "username": "Premarket Momentum",
        "content": content,
        "allowed_mentions": allowed_mentions,
        "embeds": [embed],
    }
    return validate_discord_payload(payload)


def _rank_lines(rows: list[dict[str, Any]], *, side: str) -> str:
    output: list[str] = []
    for index, row in enumerate(rows, start=1):
        driver_key = "top_driver_ticker" if side == "top" else "bottom_driver_ticker"
        driver = _safe_text(row.get(driver_key), 20) or "n/a"
        up_pct = _number(row.get("up_pct"))
        up_text = "n/a" if up_pct is None else f"{up_pct:.0%}"
        relative = _pct_decimal(row.get("headline_relative_return_1d"))
        group_name = _safe_text(row.get("group_name"), 72) or _safe_text(
            row.get("group_id"), 72
        )
        group_name = group_name.replace("*", "\\*").replace("`", "\\`")
        line = (
            f"{index}. **{group_name}** "
            f"{_pct_decimal(row.get('robust_ew_return_1d'))} · 上涨 {up_text} · "
            f"{row.get('n_valid', 0)}/{row.get('n_expected', 0)} · 相对基准 {relative} · 驱动 {driver}"
        )
        candidate = "\n".join([*output, line])
        if len(candidate) > 1024:
            break
        output.append(line)
    return "\n".join(output) if output else "无可排名数据"


def _level_embed(
    level_report: dict[str, Any],
    context: PremarketContext,
    *,
    label: str,
    color: int,
) -> dict[str, Any]:
    quality = level_report.get("quality_summary") or {}
    coverage = _number(quality.get("count_coverage"))
    coverage_text = "n/a" if coverage is None else f"{coverage:.1%}"
    warning = level_report.get("warning")
    description = (
        f"{context.source_session} EOD · ROBUST_EW / MAD winsor · "
        f"覆盖 {quality.get('n_valid', 0)}/{quality.get('n_expected', 0)} ({coverage_text}) · "
        f"可排名 {quality.get('n_groups_ranked', 0)}"
    )
    if warning:
        description += "\n⚠️ " + _safe_text(warning, 350)
    return {
        "title": f"{label} · 今日分类涨跌",
        "description": description,
        "color": color,
        "fields": [
            {
                "name": "领涨",
                "value": _rank_lines(list(level_report.get("top") or []), side="top"),
                "inline": False,
            },
            {
                "name": "领跌",
                "value": _rank_lines(list(level_report.get("bottom") or []), side="bottom"),
                "inline": False,
            },
        ],
        "footer": {
            "text": _safe_text(
                f"run {level_report.get('run_id')} · taxonomy {level_report.get('taxonomy_version')} · "
                f"delivery sector:{context.target_session}",
                500,
            )
        },
        "timestamp": context.generated_at,
    }


def _truncate_rank_value(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"[:limit]
    boundary = value.rfind("\n", 0, limit - 1)
    if boundary >= 16:
        return value[:boundary] + "\n…"
    return value[: limit - 1] + "…"


def _fit_embeds_budget(embeds: list[dict[str, Any]], *, budget: int = 5_500) -> None:
    """Trim only pathological ranking text while retaining every level/side."""
    while sum(_embed_text_length(embed) for embed in embeds) > budget:
        candidates = [
            field
            for embed in embeds
            for field in (embed.get("fields") or [])
            if len(str(field.get("value") or "")) > 64
        ]
        if not candidates:
            break
        field = max(candidates, key=lambda item: len(str(item.get("value") or "")))
        value = str(field.get("value") or "")
        excess = sum(_embed_text_length(embed) for embed in embeds) - budget
        target = max(64, len(value) - max(1, excess))
        shortened = _truncate_rank_value(value, target)
        if len(shortened) >= len(value):
            shortened = value[:63] + "…"
        field["value"] = shortened


def build_sector_rotation_payload(
    report: dict[str, Any],
    context: PremarketContext,
    settings: PremarketDigestSettings,
) -> dict[str, Any]:
    content, allowed_mentions = _mentions(
        settings.role_for(DigestChannel.SECTOR_ROTATION),
        "板块/行业强弱日报已更新",
        enabled=True,
    )
    levels = report.get("levels") or {}
    embeds: list[dict[str, Any]] = []
    if "sector" in levels:
        embeds.append(
            _level_embed(levels["sector"], context, label="Sector 板块", color=0x5865F2)
        )
    if "sub_industry" in levels:
        embeds.append(
            _level_embed(
                levels["sub_industry"],
                context,
                label="Sub-industry 细分行业",
                color=0x8B5CF6,
            )
        )
    if report.get("partial"):
        unavailable = ", ".join(sorted((report.get("errors") or {}).keys()))
        embeds[0]["description"] += (
            f"\n⚠️ 本次仅发布通过 T-1 门槛的层级；未通过：{_safe_text(unavailable, 120)}"
        )
    embeds[0]["title"] = f"板块/行业强弱日报 · {context.target_session} 开盘前"
    embeds[-1]["footer"]["text"] += " · 单日强弱并非中期行业动量 · 研究信息，不构成交易建议"
    _fit_embeds_budget(embeds)
    return validate_discord_payload(
        {
            "username": "Sector Rotation",
            "content": content,
            "allowed_mentions": allowed_mentions,
            "embeds": embeds,
        }
    )


def payload_markdown(payload: dict[str, Any]) -> str:
    """Human-readable, secret-free dry-run preview."""
    lines: list[str] = []
    if payload.get("content"):
        lines.append(str(payload["content"]))
    for embed in payload.get("embeds") or []:
        lines.extend(
            [
                f"# {embed.get('title', '')}",
                str(embed.get("description") or ""),
            ]
        )
        for field in embed.get("fields") or []:
            lines.extend([f"## {field.get('name', '')}", str(field.get("value") or "")])
        footer = (embed.get("footer") or {}).get("text")
        if footer:
            lines.append(f"_{footer}_")
    return "\n\n".join(line for line in lines if line).strip() + "\n"


__all__ = [
    "build_momentum_payload",
    "build_sector_rotation_payload",
    "payload_markdown",
]
