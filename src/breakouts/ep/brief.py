"""Read-only disclosure briefs, independent of financial extraction and model calls."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import urlsplit

from .catalyst import classify_catalyst
from .dossier import candidate_dossier
from .models import digest, timestamp

VERSION = "ep-catalyst-brief-v1"
EVENTS = {"EARNINGS": "财报披露", "M_AND_A": "并购相关披露", "DEAL_TERMINATION": "交易终止相关披露",
          "COMMERCIAL_CONTRACT": "合同相关披露", "GUIDANCE": "指引相关披露",
          "EARNINGS_CALENDAR": "财报日历线索", "LEGAL_NOTICE": "法律通知", "UNKNOWN": "事件类型待核查"}
ROLES = {"REPORTING_COMPANY": "报告公司", "ACQUIRER": "收购方", "ACQUISITION_TARGET": "被收购方",
         "CONTRACT_AWARDEE": "合同获授方", "TRANSACTION_PARTY_UNRESOLVED": "交易方角色待核查", "UNKNOWN": "待核查"}
TIMING = {"CURRENT_WINDOW_DATE_ONLY": "公告日期位于本交易观察窗口，精确发布时间未核准。",
          "BOUNDARY_RELEASE_TIME_UNVERIFIED": "公告日期位于观察窗口边界，无法确认是否在前次收盘后发布。",
          "STALE_FOR_CURRENT_EVENT_WINDOW": "旧公告，不作为本观察窗口的新催化。",
          "PROVIDER_DATELINE_DATE_CONFLICT": "供应商时间与公告日期冲突，不能认定为新消息。",
          "FUTURE_ANNOUNCEMENT_DATE_CONFLICT": "公告日期晚于观察时点，存在时间冲突。",
          "ANNOUNCEMENT_DATE_UNVERIFIED": "公告日期未核准，不能认定为新消息。",
          "EXCHANGE_CALENDAR_UNAVAILABLE": "交易日历不可用，时效未核准。",
          "PROVIDER_PUBLICATION_IN_FUTURE": "供应商发布时间晚于观察时点，不能认定为当时已知消息。"}
TIMING["PROVIDER_TIMESTAMP_UNVERIFIED"] = "供应商时间格式不完整，发布时间未核准。"


def _date(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    timestamp(date)
    return date


def _url(value):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    if re.search(r"(?:key|token|secret|signature|password)=", parsed.query, re.I):
        return None
    return value


def source_brief(source, as_of, calendar=None):
    timestamp(as_of)
    parsed = source.get("parsed") or {}
    result = source["result"]
    if parsed.get("status") != "EXTRACTED" or result.get("verification", {}).get("status") != "DOCUMENT_MATCHED":
        raise ValueError("BRIEF_REQUIRES_ALIGNED_ORIGINAL")
    for field in (source.get("observed_at"), result.get("retrieved_at")):
        if field is not None and _date(field) > as_of:
            raise ValueError("BRIEF_SOURCE_NOT_YET_OBSERVED")
    if source.get("observed_at") is None:
        raise ValueError("BRIEF_SOURCE_RECEIPT_REQUIRED")
    classified = classify_catalyst(source, as_of, calendar)
    freshness = classified["freshness"]
    if source.get("published_at"):
        try:
            if _date(source["published_at"]) > as_of:
                freshness = {**freshness, "status": "PROVIDER_PUBLICATION_IN_FUTURE"}
        except (ValueError, TypeError):
            freshness = {**freshness, "status": "PROVIDER_TIMESTAMP_UNVERIFIED"}
    direct = classified["relation"] == "DIRECT_COMPANY_DISCLOSURE_PROPOSAL"
    event = EVENTS.get(classified["type"], EVENTS["UNKNOWN"])
    description = (f"发现公司自身的{event}，属于可核查的事件线索。" if direct
                   else f"发现{event}线索，但公司主体或事件角色仍需核查。")
    risks = ["PRICE_CAUSALITY_NOT_VERIFIED", "PREMARKET_LIQUIDITY_NOT_VERIFIED", "NO_OPENING_TRIGGER_CONFIRMATION"]
    if not direct:
        risks.append("ISSUER_OR_EVENT_ROLE_UNRESOLVED")
    if freshness["status"] != "CURRENT_WINDOW_DATE_ONLY":
        risks.append(freshness["status"])
    return {"source_id": source["source_id"], "document_id": source["document_id"],
            "text_revision": parsed["text_revision"], "ticker": source["ticker"],
            "source_url": _url(result.get("final_url")), "source_title": parsed.get("title"),
            "title_semantics": "SOURCE_TITLE_NOT_ENDORSED_FINANCIAL_CLAIM",
            "source_received_at": result.get("retrieved_at", source["observed_at"]),
            "provider_published_at": source.get("published_at"),
            "description": description, "event_type": classified["type"], "role": classified["role"],
            "relation": classified["relation"], "evidence": classified["evidence"],
            "freshness": freshness, "timing_note": TIMING[freshness["status"]],
            "risk_codes": risks, "financial_claims": [], "financial_claim_policy": "WITHHELD_UNTIL_VERIFIED",
            "grade": None, "eligible_for_rating": False, "price_cause_verified": False}


def candidate_brief(store, symbol, *, run_id=None, as_of=None, calendar=None, max_sources=5):
    if type(max_sources) is not int or not 1 <= max_sources <= 10:
        raise ValueError("BRIEF_MAX_SOURCES_MUST_BE_1_TO_10")
    as_of = as_of or datetime.now(timezone.utc)
    timestamp(as_of)
    dossier = candidate_dossier(store, symbol, run_id=run_id, as_of=as_of)
    entries, seen = [], set()
    for source in dossier["sources"]:
        identity = (source["document_id"], source["text_revision"])
        if identity in seen:
            continue
        seen.add(identity)
        entries.append(source)
    # Bound expensive source reads. This ordering is receipt order, not freshness proof.
    entries.sort(key=lambda s: (s["received_at"], s["source_id"]), reverse=True)
    selected = entries[:max_sources]
    briefs = [source_brief(store.source_detail(e["source_id"], as_of=as_of), as_of, calendar) for e in selected]
    supported = {b["document_id"] for b in briefs}
    aligned = {e["document_id"] for e in entries}
    excluded = [{"document_id": event["document_id"], "reason": "LEGAL_NOTICE_NOT_OPERATING_CATALYST"}
                for event in dossier["events"] if event["event_type_hint"] == "LEGAL_NOTICE"]
    hints = [{"document_id": event["document_id"], "revision_id": event["revision_id"],
              "source_title": event["title"], "source_url": _url(event["source_url"]),
              "event_type_hint": event["event_type_hint"], "provider_published_at": event["provider_published_at"],
              "first_seen_at": event["first_seen_at"],
              "status": "ALIGNED_SOURCE_OMITTED" if event["document_id"] in aligned else "HEADLINE_ONLY_UNVERIFIED",
              "description": ("已有匹配原文，但本次因展示上限未展开。" if event["document_id"] in aligned else
                              "仅有新闻或日历线索，尚无匹配原文；不能据此认定直接催化或超预期。")}
             for event in dossier["events"] if event["document_id"] not in supported and event["event_type_hint"] != "LEGAL_NOTICE"]
    hints.sort(key=lambda e: (e["provider_published_at"] or "", e["document_id"]), reverse=True)
    omitted_hints = max(0, len(hints) - 5)
    hints = hints[:5]
    result = {"version": VERSION, "ticker": dossier["ticker"], "run_id": dossier["run_id"],
              "as_of": timestamp(as_of), "collection_scope": dossier["collection_scope"],
              "status": "DISCLOSURE_BRIEF_READY" if briefs else "HEADLINE_ONLY" if hints else "NO_VISIBLE_EVIDENCE",
              "dossier_status": dossier["status"], "sources": briefs, "headline_hints": hints,
              "excluded_hints": excluded,
              "coverage": {"aligned_sources": len(entries), "included_sources": len(selected),
                           "omitted_sources": len(entries) - len(selected), "omitted_headlines": omitted_hints,
                           "excluded_legal_notices": len(excluded), "exhaustiveness_verified": False},
              "financial_claims": [], "financial_policy": "NO_UNVERIFIED_NUMBERS_OR_BEAT_RAISE_CLAIMS",
              "human_assertions_not_promoted": len(dossier["facts"]),
              "grade": None, "eligible_for_rating": False, "price_cause_verified": False,
              "renderer": "DETERMINISTIC_TEMPLATE_NOT_LLM_ANALYSIS", "external_requests": 0,
              "budget_writes": 0, "delivery": "DISABLED_SHADOW_ONLY"}
    result["brief_id"] = digest(result)
    return result


def _plain(value):
    # The text renderer emits plain text, not executable HTML/Markdown or chat mentions.
    text = " ".join(str(value or "未提供").split())
    return text.replace("@", "[at]").replace("<", "(").replace(">", ")")


def render_brief(brief):
    lines = [f"{brief['ticker']} | EP 事件摘要（观察，不是买入信号）", f"观察时点：{brief['as_of']}",
             f"数据范围：{_plain(brief['collection_scope'])}"]
    for item in brief["sources"]:
        lines.extend(["", item["description"], item["timing_note"],
                      f"角色线索：{ROLES.get(item['role'], '待核查')}；不代表已证明股价上涨原因。",
                      f"公告日期：{(item['freshness'].get('announcement') or {}).get('date', '未核准')}",
                      f"来源标题（未背书）：{_plain(item['source_title'])}",
                      f"来源：{item['source_url'] or '无可展示的公开 HTTPS 链接'}",
                      f"原文取得时间：{item['source_received_at']}",
                      f"供应商发布时间：{item['provider_published_at'] or '未提供'}"])
    for item in brief["headline_hints"]:
        title = item["source_title"] or EVENTS.get(item["event_type_hint"], "无标题的事件线索")
        lines.extend(["", item["description"], f"标题（未核准）：{_plain(title)}",
                      f"来源：{item['source_url'] or '无可展示的公开 HTTPS 链接'}",
                      f"首次发现：{item['first_seen_at']}", f"供应商时间：{item['provider_published_at'] or '未提供'}"])
    if not brief["sources"] and not brief["headline_hints"]:
        lines.append("该观察范围内没有可展示证据；不等于该股票没有消息。")
    if brief["coverage"]["omitted_sources"]:
        lines.append(f"另有 {brief['coverage']['omitted_sources']} 份原文未展示，不能视为完整覆盖。")
    if brief["coverage"]["omitted_headlines"]:
        lines.append(f"另有 {brief['coverage']['omitted_headlines']} 条线索未展示。")
    if brief["excluded_hints"]:
        lines.append(f"已折叠 {len(brief['excluded_hints'])} 条法律通知，不当作经营催化。")
    lines.extend(["", "未核准的财务数字、超预期比例和一次性收益调整不写入分析结论。",
                  "尚未确认价格上涨原因、盘前流动性或开盘触发条件；未评级、未发送。"])
    return "\n".join(lines)
