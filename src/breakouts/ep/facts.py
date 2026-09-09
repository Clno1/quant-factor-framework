"""Bounded, deterministic financial proposals from archived announcement paragraphs.

Only explicit prose and a narrow, fully labelled summary-table grammar are supported.
Unsupported financial layouts remain evidence gaps, never guessed column assignments.
"""
from __future__ import annotations

from decimal import Decimal
import re

from .models import digest

VERSION = "ep-financial-proposals-v1"
NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
MONEY = rf"\$\s*(?:\({NUMBER}\)|-?{NUMBER})(?!\d|[,.]\d)"
PERIOD = re.compile(r"\b(?:(?:first|second|third|fourth)\s+quarter(?:\s+of)?\s+(?:fiscal(?:\s+year)?\s+)?20\d{2}|Q[1-4]\s+FY\s+20\d{2}|(?:full[- ]year|fiscal year|FY)\s+20\d{2})\b", re.I)
STOP = re.compile(r"^(?:About\b|Forward[- ]Looking Statements|Non-GAAP Financial Measures|Conference Call|Earnings Conference Call|For Further Information|Condensed Consolidated)", re.I)
EPS = re.compile(r"\b(?:EPS|(?:earnings|net (?:income|loss)) per (?:diluted |basic )?share|(?:diluted|basic) (?:net income|earnings) per share)\b", re.I)
REVENUE = re.compile(r"\b(?:revenue|net sales)\b", re.I)
SPECIAL = re.compile(r"\b(?:one[- ]time|non[- ]recurring|tax benefit|tariff refund|refunds of .*?tariffs|repurchas\w*|buyback)\b", re.I)


def anchor(paragraph: dict) -> dict:
    return {"paragraph_id": paragraph["id"], "quote": paragraph["text"]}


def amount(value: str) -> str:
    cleaned = value.replace("$", "").replace(",", "").replace(" ", "")
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return format(Decimal(cleaned), "f")


def basis(text: str) -> str:
    non_gaap = bool(re.search(r"\bnon[- ]GAAP\b", text, re.I))
    gaap = bool(re.search(r"(?<!non-)(?<!non )\bGAAP\b", text, re.I))
    if non_gaap and gaap:
        return "UNKNOWN"
    if non_gaap:
        return "NON_GAAP"
    if gaap:
        return "GAAP"
    return "ADJUSTED_UNSPECIFIED" if re.search(r"\badjusted\b", text, re.I) else "UNKNOWN"


def share_basis(text: str) -> str:
    found = {word.upper() for word in re.findall(r"\b(basic|diluted)\b", text, re.I)}
    return next(iter(found)) if len(found) == 1 else "UNKNOWN"


def _proposal(metric, text_value, paragraph, *, period=None, contexts=(), kind="ACTUAL",
              scale=None, label=None, scope="REPORTING_COMPANY_PROPOSAL", measure="LEVEL"):
    label = label or paragraph["text"]
    amounts = re.findall(MONEY, text_value)
    values = [amount(v) for v in amounts]
    if not values and measure == "GROWTH_RATE":
        values = [format(Decimal(v), "f") for v in re.findall(NUMBER, text_value)]
    multiplier = {"million": Decimal(1_000_000), "billion": Decimal(1_000_000_000),
                  "thousand": Decimal(1000)}.get(scale, Decimal(1))
    unit = "PERCENT" if measure == "GROWTH_RATE" else "DOLLAR_PER_SHARE" if metric == "EPS" else "DOLLAR" if scale else "UNKNOWN"
    row = {"metric": metric, "value_text": text_value, "values": values,
           "normalized_values": [format(Decimal(v) * multiplier, "f") for v in values],
           "unit": unit, "currency": "NOT_APPLICABLE" if measure == "GROWTH_RATE" else "DOLLAR_SYMBOL_CURRENCY_UNVERIFIED",
           "scale": scale, "measure": measure, "period_text": period,
           "basis": basis(label), "share_basis": share_basis(label) if metric == "EPS" else "NOT_APPLICABLE",
           "value_kind": kind, "subject_scope": scope, "evidence": [anchor(paragraph), *[anchor(p) for p in contexts]],
           "semantics": "RULE_EXTRACTED_PROPOSAL_REQUIRES_REVIEW", "comparison_status": "NOT_COMPUTED"}
    missing = []
    if not period:
        missing.append("PERIOD_UNRESOLVED")
    elif not re.search(r"20\d{2}", period):
        missing.append("FISCAL_YEAR_UNRESOLVED")
    if unit == "UNKNOWN":
        missing.append("UNIT_UNRESOLVED")
    if measure != "GROWTH_RATE":
        missing.append("CURRENCY_NOT_EXPLICITLY_VERIFIED")
    if re.search(r"\bnet loss\b", label, re.I) and any(Decimal(v) > 0 for v in values):
        missing.append("LOSS_SIGN_REVIEW_REQUIRED")
        row["normalized_values"] = None
    if len(values) == 2 and Decimal(values[0]) > Decimal(values[1]):
        missing.append("RANGE_ORDER_REVIEW_REQUIRED")
        row["normalized_values"] = None
    if metric == "EPS" and row["basis"] in {"UNKNOWN", "ADJUSTED_UNSPECIFIED"}:
        missing.append("EPS_ACCOUNTING_BASIS_UNRESOLVED")
    if metric == "EPS" and row["share_basis"] == "UNKNOWN":
        missing.append("EPS_SHARE_BASIS_UNRESOLVED")
    if scope != "REPORTING_COMPANY_PROPOSAL":
        missing.append("SUBJECT_REVIEW_REQUIRED")
    row["missing_context"] = missing
    row["proposal_id"] = digest(row)
    return row


def _summary_table(rows, index):
    """Recognize explicit Qn FY columns only; require exact values/column cardinality."""
    header = rows[index]
    text = header["text"]
    columns = re.findall(r"(?:Q[1-4]\s+)?FY\s+20\d{2}(?:\s+Guidance)?", text, re.I)
    remainder = re.sub(r"(?:Q[1-4]\s+)?FY\s+20\d{2}(?:\s+Guidance)?|Y/Y Change", "", text, flags=re.I).strip()
    if len(columns) != 2 or remainder or columns[0].casefold() == columns[1].casefold():
        return [], set(), []
    guidance = all("guidance" in c.lower() for c in columns)
    if any("guidance" in c.lower() for c in columns) != guidance:
        return [], set(), [{"paragraph_id": header["id"], "reason": "MIXED_TABLE_COLUMN_KINDS"}]
    unit_row = next((p for p in reversed(rows[max(0, index - 3):index])
                     if re.search(r"in millions.*except.*per share", p["text"], re.I)), None)
    result, handled, gaps = [], set(), []
    for p in rows[index + 1:index + 18]:
        line = p["text"]
        if "$" not in line and not re.match(r"(?:Non-GAAP|GAAP)", line, re.I):
            break
        first = line.find("$")
        label = line[:first].strip() if first >= 0 else line
        metric = "REVENUE" if re.fullmatch(r"Revenue|Net sales", label, re.I) else "EPS" if EPS.search(label) else None
        if not metric:
            continue
        handled.add(p["id"])
        tail = line[first:]
        contexts = [header] + ([unit_row] if unit_row else [])
        if guidance:
            value_pattern = rf"{MONEY}\s*(?:-|to)\s*{MONEY}"
            values = re.findall(value_pattern, tail, re.I)
            valid = len(values) == 2 and not re.sub(value_pattern, "", tail, flags=re.I).strip()
        else:
            values = re.findall(MONEY, tail)
            suffix = re.sub(MONEY, "", tail)
            # Optional explicitly labelled Y/Y change may be dollars, percent or a dash.
            has_change = "Y/Y Change" in text
            valid = len(values) == (3 if has_change and len(values) == 3 else 2)
            valid = valid and bool(re.fullmatch(r"\s*(?:\$?\s*[\u2014-]|-?\d+(?:\.\d+)?\s*%)?\s*", suffix))
            valid = valid and (has_change or len(values) == 2 and not suffix.strip())
            valid = valid and (not has_change or len(values) == 3 or bool(suffix.strip()))
            values = values[:2]
        if not valid or metric == "REVENUE" and unit_row is None:
            gaps.append({"paragraph_id": p["id"], "reason": "TABLE_VALUE_OR_UNIT_ALIGNMENT_UNRESOLVED"})
            continue
        for column, value in zip(columns, values):
            result.append(_proposal(metric, value, p, period=column, contexts=contexts,
                kind="COMPANY_GUIDANCE" if guidance else "ACTUAL", scale="million" if metric == "REVENUE" else None,
                label=label))
    return result, handled, gaps


def extract_financial_proposals(source: dict) -> dict:
    parsed = source.get("parsed") or {}
    if (parsed.get("status") != "EXTRACTED" or
            source.get("result", {}).get("verification", {}).get("status") != "DOCUMENT_MATCHED"):
        raise ValueError("Financial extraction requires aligned, extracted original text")
    rows = parsed["paragraphs"]
    if len(rows) > 4000 or sum(len(p["text"]) for p in rows) > 250_000:
        raise ValueError("Financial extraction source exceeds bounded scope")
    stop = next((i for i, p in enumerate(rows) if STOP.search(p["text"])), len(rows))
    rows = rows[:stop]
    facts, gaps, risks, handled = [], [], [], set()
    title = parsed.get("title", "")
    acquisition = bool(re.search(r"acquir|acquisition|merger", title, re.I))
    period_row = next((p for p in rows[:10] if PERIOD.search(p["text"]) and "$" not in p["text"]
                       and not re.search(r"guidance|outlook|expects", p["text"], re.I)), None)
    period = PERIOD.search(period_row["text"])[0] if period_row else None
    for i, p in enumerate(rows):
        table, ids, failures = _summary_table(rows, i)
        facts.extend(table)
        handled.update(ids)
        gaps.extend(failures)
    guidance_context = None
    for p in rows:
        text = p["text"]
        if "$" not in text and len(text) < 250 and re.search(r"(?:quarter|year|FY).*?(?:guidance|outlook|expects)", text, re.I):
            guidance_context = p
        if len(text) < 200 and PERIOD.search(text) and re.search(r"Results|Highlights", text, re.I):
            period_row, period = p, PERIOD.search(text)[0]
            guidance_context = None
        matches = list(SPECIAL.finditer(text))
        if matches:
            risk_type = "CAPITAL_RETURN_NOT_ONE_TIME_EARNINGS" if all(re.match(r"repurchas|buyback", m[0], re.I) for m in matches) else "SPECIAL_ITEM_REVIEW_REQUIRED"
            amounts = []
            for match in re.finditer(rf"(?P<value>{MONEY})(?:\s+(?P<scale>million|billion)|\s+(?:per\s+(?:diluted\s+)?share))", text, re.I):
                amounts.append({"value_text": match[0], "value": amount(match["value"]),
                                "scale": match["scale"], "allocation": "UNASSIGNED_PARAGRAPH_AMOUNT"})
            risks.append({"kind": risk_type, "evidence": anchor(p), "amount_mentions": amounts,
                          "automatic_adjustment": "DISABLED", "period_allocation": "REVIEW_REQUIRED"})
        if p["id"] in handled:
            continue
        context = [period_row] if period_row and period_row != p else []
        leading = re.split(r"\bcompared (?:with|to)\b|\bas compared\b", text, maxsplit=1, flags=re.I)[0]
        local_period = PERIOD.search(leading)
        row_period = local_period[0] if local_period else period
        guidance_text = re.sub(r"\b(?:above|below|exceeded|exceeds|beat) (?:our |the )?(?:outlook|guidance)\b", "", leading, flags=re.I)
        local_guidance = re.search(r"\b(?:outlook|guidance|expects?|to be between)\b", guidance_text, re.I)
        is_guidance = bool(local_guidance) or guidance_context is not None
        kind = "COMPANY_GUIDANCE" if is_guidance else "ACTUAL"
        if is_guidance:
            # Bullet outlooks must not lend their period to a later historical results section.
            if local_guidance and guidance_context is None:
                short_period = re.search(r"\b(?:full[- ]year|(?:first|second|third|fourth) quarter)\b", leading, re.I)
                row_period = local_period[0] if local_period else short_period[0] if short_period else None
            elif guidance_context:
                context = [guidance_context]
                match = PERIOD.search(guidance_context["text"])
                row_period = match[0] if match else None
        if re.search(r"^.*?summary of results|today (?:reported|announced).*results", text, re.I):
            guidance_context = None
            kind = "ACTUAL"
        made = []
        scope = "TRANSACTION_SUBJECT_UNRESOLVED" if acquisition else "REPORTING_COMPANY_PROPOSAL"
        revenue = re.search(rf"\b(?P<label>(?:total )?revenue|net sales)\s+(?:was|of|to be between)\s+(?P<value>{MONEY})(?:\s+(?P<scale>million|billion))?(?:\s+(?:and|to)\s+(?P<upper>{MONEY})\s*(?P<upper_scale>million|billion)?)?", text, re.I)
        if revenue:
            prefix = text[:revenue.start()].strip(" \u2022\u00b7")
            # Segment, target-company and third-party amounts are not consolidated revenue.
            actual_scope = scope if not prefix or re.fullmatch(r"Record .*quarter", prefix, re.I) else "SUBJECT_OR_SEGMENT_UNRESOLVED"
            scale = (revenue["scale"] or revenue["upper_scale"] or "").lower() or None
            value = text[revenue.start("value"):revenue.end("upper")] if revenue["upper"] else revenue["value"]
            if revenue["scale"] and revenue["upper_scale"] and revenue["scale"].lower() != revenue["upper_scale"].lower():
                gaps.append({"paragraph_id": p["id"], "reason": "MIXED_RANGE_UNITS"})
            else:
                made.append(_proposal("REVENUE", value, p, period=row_period, contexts=context,
                    kind=kind, scale=scale, scope=actual_scope, label=revenue["label"]))
                growth = re.search(r"\b(up|down|increase of|decrease of)\s+(\d+(?:\.\d+)?%)(?:\s+(?:as compared )?to last year|\s+year-over-year)", text[revenue.end():], re.I)
                if growth:
                    proposal = _proposal("REVENUE", growth[2], p, period=row_period, contexts=context,
                        kind=kind, scope=actual_scope, measure="GROWTH_RATE", label=revenue["label"])
                    proposal["growth_direction"] = "DECREASE" if growth[1].lower() in {"down", "decrease of"} else "INCREASE"
                    proposal["comparison_basis"] = "YEAR_OVER_YEAR_COMPANY_REPORTED"
                    proposal["proposal_id"] = digest({k: v for k, v in proposal.items() if k != "proposal_id"})
                    made.append(proposal)
        target = re.search(rf"(?P<subject>[A-Za-z][A-Za-z ]+)[\u2019']s financial performance includes estimated revenue for (?P<period>FY 20\d{{2}}) of (?P<qualifier>over |approximately )?(?P<value>{MONEY}) (?P<scale>million|billion)", text, re.I)
        if acquisition and target:
            proposal = _proposal("REVENUE", target["value"], p, period=target["period"],
                kind="COMPANY_ESTIMATE", scale=target["scale"].lower(), scope="TRANSACTION_OTHER_ENTITY_PROPOSAL", label="revenue")
            proposal["subject_text"] = target["subject"]
            proposal["qualifier"] = (target["qualifier"] or "").strip() or None
            proposal["proposal_id"] = digest({k: v for k, v in proposal.items() if k != "proposal_id"})
            made.append(proposal)
        eps = EPS.search(text)
        eps_value = re.search(rf"(?:\bof|\bwas|\bbe between)\s+(?P<a>{MONEY})(?:\s+(?:and|to)\s+(?P<b>{MONEY}))?", text[eps.end():], re.I) if eps else None
        if eps and eps_value:
            # Do not jump over another metric to attach its dollar value to EPS.
            between = text[eps.end():eps.end() + eps_value.start()]
            if len(between) <= 100 and not re.search(r"revenue|repurchas|cash|margin|benefit|in the range", between, re.I):
                value = text[eps.end() + eps_value.start("a"):eps.end() + eps_value.end("b")] if eps_value["b"] else eps_value["a"]
                made.append(_proposal("EPS", value,
                    p, period=row_period, contexts=context, kind=kind, label=text[:eps.end()], scope=scope))
        # Net income prose often expresses EPS as "or $0.50 per diluted share".
        per_share = re.search(rf"\bor\s+(?P<a>{MONEY})\s+per\s+(?P<share>diluted|basic)\s+share", text, re.I)
        if not eps and per_share and re.search(r"\bnet income\b", text, re.I):
            made.append(_proposal("EPS", per_share["a"], p, period=row_period, contexts=context,
                kind=kind, label=text[:per_share.end()], scope=scope))
        tariff = re.search(rf"tariff refund benefit of (?:approximately )?(?P<income>{MONEY}) (?P<scale>million|billion) on a pre-tax basis and (?P<eps>{MONEY}) per diluted share", text, re.I)
        if tariff:
            for metric, value, scale in (("TARIFF_REFUND_PRETAX_IMPACT", tariff["income"], tariff["scale"].lower()),
                                         ("EPS", tariff["eps"], None)):
                proposal = _proposal(metric, value, p, period=row_period, contexts=context,
                    kind="SPECIAL_ITEM_IMPACT", scale=scale, label="per diluted share" if metric == "EPS" else "pre-tax")
                proposal["item_text"] = "tariff refund benefit"
                proposal["automatic_adjustment"] = "DISABLED_REQUIRES_ACCOUNTING_REVIEW"
                proposal["proposal_id"] = digest({k: v for k, v in proposal.items() if k != "proposal_id"})
                made.append(proposal)
        facts.extend(made)
        if not made and "$" in text and (REVENUE.search(text) or EPS.search(text)):
            gaps.append({"paragraph_id": p["id"], "reason": "UNSUPPORTED_SENTENCE_OR_TABLE_LAYOUT"})
        if made and re.search(r"compared (?:with|to)|last year|year-over-year|sequentially", text, re.I):
            gaps.append({"paragraph_id": p["id"], "reason": "PROSE_COMPARISON_VALUES_NOT_EXTRACTED"})
    unique = {row["proposal_id"]: row for row in facts}
    return {"version": VERSION, "source_id": source["source_id"], "document_id": source["document_id"],
            "text_revision": parsed["text_revision"], "proposals": list(unique.values()), "special_items": risks,
            "coverage_gaps": gaps, "paragraphs_in_scope": len(rows), "total_paragraphs": len(parsed["paragraphs"]),
            "scope": "ANNOUNCEMENT_SUMMARY_BEFORE_BOILERPLATE_NOT_FULL_FINANCIAL_STATEMENTS",
            "comparisons": {"earnings_surprise": "CONSENSUS_AND_ASOF_REQUIRED",
                            "guidance_change": "PRIOR_COMPARABLE_GUIDANCE_REQUIRED"},
            "financial_semantics_verified": False, "eligible_for_rating": False}
