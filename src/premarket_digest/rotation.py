"""Upper-level, read-only composition of rotation and existing breakout output.

The group domain never imports this module or a strategy. Candidate associations
are frozen into the writer's snapshot so Web and Discord show the same evidence.
"""
from __future__ import annotations

from copy import deepcopy
import math
from urllib.parse import quote

from src.alerts.discord import validate_discord_payload
from src.group_analytics.rotation.store import RotationStore
from .models import SourceGateError

STATUS_ZH = {"READY": "临近突破", "BREAKOUT": "突破观察", "SETUP": "形态准备"}
CONFIRMATION_ZH = {
    "READY": "仍需有效突破既有参考位并核对成交量；当前只是临近突破",
    "BREAKOUT": "核对价格能否守住既有突破参考位，并排除开盘跳空后快速跌回",
    "SETUP": "形态仍在准备，先等待收敛与突破条件完善，不因主题强就提前确认",
}
SECTORS = {
    "sector_technology": "Technology", "sector_financials": "Financial Services",
    "sector_healthcare": "Healthcare", "sector_industrials": "Industrials",
    "sector_discretionary": "Consumer Cyclical", "sector_staples": "Consumer Defensive",
    "sector_energy": "Energy", "sector_materials": "Basic Materials",
    "sector_utilities": "Utilities", "sector_realestate": "Real Estate",
    "sector_communications": "Communication Services",
}
ACTION_ZH = {
    "unavailable": "数据不足",
    "focus": "持续领先", "defensive": "相对抗跌", "recover": "正在修复",
    "weak": "持续落后", "neutral": "与基准同步",
    "priority": "优先核对", "price_watch": "价格领先／待广度确认",
    "watch": "转强观察", "extended": "延伸偏大／不追涨",
    "caution": "降温或落后", "wait": "等待确认",
}
ELIGIBLE_ACTIONS = {"focus", "recover", "priority", "price_watch", "watch"}
LINKAGE_PENDING_REASONS = {"个股关联待后续阶段", "个股关联未启用"}
RISK_FLAG_ZH = {
    "EXTENDED": "延伸偏大",
    "BELOW_ABSOLUTE_MA20": "低于绝对20日均线",
    "ABSOLUTE_DOWNTREND": "绝对下跌",
}


def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def attach_candidates(snapshot, report=None, *, unavailable_reason=None):
    result = deepcopy(snapshot)
    valid_report = isinstance(report, dict) and report.get("source_session") == result["source_session"]
    result["candidate_linkage"] = {
        "status": "available" if valid_report else "unavailable",
        "source_session": report.get("source_session") if isinstance(report, dict) else None,
        "input_fingerprint": report.get("input_fingerprint") if valid_report else None,
        "universe": report.get("universe") if valid_report else None,
        "reason": None if valid_report else (unavailable_reason or "没有同日突破扫描产物"),
    }
    raw = report.get("rows", []) if valid_report else []
    universe = report.get("universe") if valid_report else None
    universe_query = "?universe=" + quote(universe, safe="") if universe in {"SP500", "US_ACTIVE"} else ""
    unique = {}
    for row in raw:
        if row.get("data_date") != result["source_session"] or row.get("status") not in STATUS_ZH:
            continue
        ticker = str(row.get("ticker", ""))
        import re
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,15}", ticker):
            continue
        if (_number(row.get("pivot")) or 0) <= 0 or (_number(row.get("close")) or 0) <= 0:
            continue
        if row.get("base_pass") is False:
            continue
        unique.setdefault(ticker, row)
    for theme in result["rows"]:
        members = {m["ticker"] for m in theme["members"]}
        sector = SECTORS.get(theme["id"])
        matched = [r for ticker, r in unique.items() if ticker in members or (sector and r.get("sector") == sector)]
        matched.sort(key=lambda r: ({"BREAKOUT": 0, "READY": 1, "SETUP": 2}[r["status"]], -(_number(r.get("score")) or 0), r["ticker"]))
        theme["candidates"] = [{"ticker": r["ticker"], "security_id": r["ticker"],
                                 "status": r["status"], "status_name": STATUS_ZH[r["status"]],
                                 "score": _number(r.get("score")), "pivot": _number(r["pivot"]),
                                 "close": _number(r["close"]), "source_session": result["source_session"],
                                 "distance_to_pivot_pct": 100 * (_number(r["close"]) / _number(r["pivot"]) - 1),
                                 "href": "/breakouts/" + quote(r["ticker"], safe="") + universe_query,
                                 "confirmation": CONFIRMATION_ZH[r["status"]] + "；本报告不含盘中确认"}
                                for r in matched[:3]]
        theme["candidate_basis"] = ("FMP同板块股票，非ETF持仓名单" if sector else
                                     "已登记篮子成员" if members else "未登记ETF成分映射")
        theme["action_name"] = ACTION_ZH.get(theme["production"]["action"], theme["production"]["action"])
        theme["confirmation"] = "观察20日相对优势能否保持，并核验成员广度与既有个股突破位"
        theme["invalidation"] = "20日相对转弱或既有突破形态失效时重新评估；不是自动止损指令"
        gaps = list(theme.get("evidence_gaps") or theme["production"].get("evidence_gaps") or [])
        if valid_report:
            if not theme["candidates"] and "NO_QUALIFIED_CANDIDATE" not in gaps:
                gaps.append("NO_QUALIFIED_CANDIDATE")
        elif (unavailable_reason or "没有同日突破扫描产物") not in LINKAGE_PENDING_REASONS:
            if "LINKAGE_FAILED" not in gaps:
                gaps.append("LINKAGE_FAILED")
        theme["evidence_gaps"] = gaps
        # Linkage/display gaps stay on the row. production.evidence_gaps remains
        # the engine snapshot so replay MATCH still holds after association.
    return result


def load_rotation_report(source_session, *, store=None, now=None):
    store = store or RotationStore()
    try:
        report = store.load()
    except Exception:
        raise SourceGateError("ROTATION_ARTIFACT_UNAVAILABLE", "轮动快照不可用") from None
    if report["source_session"] != source_session or report.get("session_status") != "FINAL":
        raise SourceGateError("ROTATION_STALE", "轮动快照不是所需的上一完整交易日")
    if now is not None:
        import pandas as pd
        if pd.Timestamp(report["generated_at"]) > pd.Timestamp(now) + pd.Timedelta(minutes=1):
            raise SourceGateError("ROTATION_FUTURE", "轮动快照生成时间异常")
    if report.get("valid_theme_count", 0) < max(1, report.get("total_theme_count", 0) * .8):
        raise SourceGateError("ROTATION_LOW_COVERAGE", "有效主题不足80%，暂停盘前摘要")
    # Discord renderer switches on this kind (not schema_version). Keep both
    # labels so a v3 snapshot is not mistaken for the old single-day group digest.
    schema = str(report.get("schema_version") or "")
    kind = "rotation_v3" if schema.startswith("rotation.v3") else "rotation_v2"
    return {**report, "kind": kind}


def _pct(value):
    n = _number(value)
    return "—" if n is None else f"{n:+.2f}%"


def _action_name(action):
    return ACTION_ZH.get(action, action)


def _risk_flags(production):
    return [flag for flag in (production.get("risk_flags") or []) if flag]


def rotation_payload(report, context, settings):
    fields = []
    for cohort, name in (("technology", "科技与AI"), ("sectors", "全市场板块")):
        eligible = [r for r in report["rows"] if r["cohort"] == cohort and r["production"]["action"] in ELIGIBLE_ACTIONS]
        eligible.sort(key=lambda r: -(r["production"].get("rs20") or 0))
        lines = []
        for r in eligible[:2]:
            p = r["production"]
            label = p.get("combined_label") or p.get("state_name")
            line = f"**{r['name']}** · {label} · {_action_name(p['action'])}\n5日 {_pct(p['rs5'])}｜20日 {_pct(p['rs20'])}｜60日 {_pct(p['rs60'])}（相对{r['benchmark']}）"
            hb = r.get("holdings_breadth") or {}
            if hb.get("breadth_kind") == "etf_holdings_observation" and hb.get("breadth_equal_weight_pct") is not None:
                equal = hb["breadth_equal_weight_pct"]
                weighted = hb.get("breadth_weighted_pct")
                line += f"\n当前持仓观测广度 等权 {equal:.0f}%"
                if weighted is not None:
                    line += f" / 加权 {weighted:.0f}%"
                line += "（持仓生效日未披露）"
            elif hb.get("status") == "HOLDINGS_OBSERVATION_STALE":
                line += "\n持仓观测过期未用，仅价格观察"
            elif hb.get("status") == "HOLDINGS_MEASUREMENT_FAILED":
                line += "\n持仓已接入但成员价格不足，仅价格观察"
            elif p.get("breadth") is not None:
                line += f"\n站20日线 {int(p['breadth_n'])}只有效样本中的 {p['breadth']:.0f}%"
                if p["breadth_n"] < 5:
                    line += "（小样本，广度不作为优先门槛）"
            else:
                line += "\n真实广度未接入，仅价格观察"
            if r.get("candidates"):
                line += "\n个股核对：" + "、".join(f"{c['ticker']}（{c['status_name']}，参考位{c['pivot']:.2f}）" for c in r["candidates"][:2])
            else:
                line += "\n暂无已关联的合格个股候选"
            lines.append(line)
        fields.append({"name": name + "｜优先观察", "value": "\n\n".join(lines) or "暂无持续领先或正在修复的方向。", "inline": False})
    risk_lines = []
    for cohort, label in (("technology", "科技"), ("sectors", "全市场")):
        rows = [r for r in report["rows"] if r["cohort"] == cohort and r["production"]["history_valid"]]
        weak = sorted((r for r in rows if r["production"]["action"] in {"weak", "caution"}),
                      key=lambda r: r["production"].get("rs20") or 0)
        extended = [r for r in rows if r["production"]["action"] == "extended"
                    or "EXTENDED" in _risk_flags(r["production"])]
        if weak:
            risk_lines.append(label + "持续落后：" + "、".join(r["name"] for r in weak[:2]))
        if extended:
            risk_lines.append(label + "延伸偏大：" + "、".join(r["name"] for r in extended[:2]))
        flagged = [r for r in rows if set(_risk_flags(r["production"])) - {"EXTENDED"}]
        extra = []
        for r in flagged[:2]:
            extra.append(r["name"] + "（" + "、".join(RISK_FLAG_ZH.get(f, f) for f in _risk_flags(r["production"]) if f != "EXTENDED") + "）")
        if extra:
            risk_lines.append(label + "其他风险：" + "、".join(extra))
    fields.append({"name": "风险与失效条件", "value":
                   ("；".join(risk_lines) or "无额外风险标签") +
                   "。若20日相对优势消失或原突破形态失效，重新评估；延伸偏大时不把强势当作追涨理由。", "inline": False})
    base = settings.dashboard_base_url.rstrip("/")
    latest = base + "/group-analytics" if base.startswith(("https://", "http://")) else None
    url = (latest + "?run=" + report["run_id"]) if latest else None
    embed = {"title": f"板块轮动 · {context.target_session} 开盘前",
             "description": (
                 f"截至 {report['source_session']} 完整收盘 · 不含实时盘前行情\n"
                 f"本消息数据截至 {report['source_session']} 收盘，链接为该时刻固定快照\n"
                 f"{report['context']['label']} · 有效主题 {report['valid_theme_count']}/{report['total_theme_count']}"
             ),
             "color": 0x386FC8, "fields": fields,
             "footer": {"text": "主题强度不是个股买点 · 研究观察，非买卖指令"}}
    if latest:
        fields.append({"name": "页面入口",
                       "value": f"固定快照见标题链接\n查看最新：{latest}",
                       "inline": False})
    if url:
        embed["url"] = url
    from .models import DigestChannel
    from .render import _mentions
    content, mentions = _mentions(settings.role_for(DigestChannel.SECTOR_ROTATION),
                                  "板块轮动观察已更新", enabled=True)
    return validate_discord_payload({"username": "Sector Rotation", "content": content or "板块轮动观察已更新",
                                     "allowed_mentions": mentions, "embeds": [embed]})
