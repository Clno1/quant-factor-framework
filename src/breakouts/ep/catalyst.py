"""Evidence-based event proposals and exchange-calendar freshness, not price causality."""
from __future__ import annotations

from datetime import datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

from .facts import anchor
from .models import timestamp

VERSION = "ep-catalyst-proposals-v1"
ET = ZoneInfo("America/New_York")
DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)\s+(\d{1,2}),\s+(20\d{2})\b", re.I)
MONTHS = {name: index for index, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def release_date(source):
    """Only datelines before an explicit issuer ticker; never use fiscal or call dates."""
    symbol = re.escape(source["ticker"])
    attribution = re.compile(r"\((?:NASDAQ|NYSE|NYSE American|Nasdaq)\s*:\s*" + symbol + r"(?:\s*\)|\s*;)", re.I)
    found = []
    for paragraph in source["parsed"]["paragraphs"][:15]:
        text = paragraph["text"]
        issuer = attribution.search(text)
        if not issuer or not re.search(r"\btoday (?:announced|reported)\b", text[issuer.end():], re.I):
            continue
        dates = list(DATE.finditer(text[:issuer.start()]))
        if len(dates) != 1:
            continue
        match = dates[0]
        try:
            day = datetime(int(match[3]), MONTHS[match[1][:3].lower()], int(match[2])).date()
        except ValueError:
            continue
        found.append({"date": day.isoformat(), "date_text": match[0], "evidence": anchor(paragraph)})
    if len({row["date"] for row in found}) != 1:
        return None
    return found[0]


def event_window(as_of, calendar=None):
    timestamp(as_of)
    if calendar is None:
        import exchange_calendars as xcals
        calendar = xcals.get_calendar("XNYS")
    day = as_of.astimezone(ET).date().isoformat()
    session = calendar.date_to_session(day, direction="next")
    if as_of >= calendar.session_close(session).to_pydatetime():
        session = calendar.next_session(session)
    previous = calendar.previous_session(session)
    return {"session": session.strftime("%Y-%m-%d"),
            "start": calendar.session_close(previous).to_pydatetime(), "end": as_of}


def freshness(source, as_of, calendar=None):
    stamp = timestamp(as_of)
    date = release_date(source)
    result = {"as_of": stamp, "announcement": date, "provider_published_at": source.get("published_at"),
              "exact_release_time_verified": False, "historical_realtime_availability_verified": False,
              "policy": "PREVIOUS_XNYS_CLOSE_TO_OBSERVATION", "status": "ANNOUNCEMENT_DATE_UNVERIFIED"}
    if not date:
        return result
    try:
        window = event_window(as_of, calendar)
    except (ImportError, ValueError, KeyError):
        result["status"] = "EXCHANGE_CALENDAR_UNAVAILABLE"
        return result
    result["session"] = window["session"]
    result["window_start"] = timestamp(window["start"])
    day = datetime.fromisoformat(date["date"]).date()
    earliest = datetime.combine(day, time.min, ET)
    latest = datetime.combine(day + timedelta(days=1), time.min, ET)
    if latest <= window["start"]:
        result["status"] = "STALE_FOR_CURRENT_EVENT_WINDOW"
    elif earliest > as_of:
        result["status"] = "FUTURE_ANNOUNCEMENT_DATE_CONFLICT"
    elif earliest >= window["start"]:
        result["status"] = "CURRENT_WINDOW_DATE_ONLY"
    else:
        result["status"] = "BOUNDARY_RELEASE_TIME_UNVERIFIED"
    value = source.get("published_at")
    if value:
        try:
            published = datetime.fromisoformat(value.replace("Z", "+00:00"))
            timestamp(published)
            result["provider_time_in_window"] = window["start"] < published <= as_of
            if published.astimezone(ET).date() != day:
                result["status"] = "PROVIDER_DATELINE_DATE_CONFLICT"
        except (ValueError, TypeError):
            result["provider_time_in_window"] = None
    return result


def classify_catalyst(source, as_of, calendar=None):
    title = source["parsed"].get("title", "")
    registry = source["result"].get("registry_entry") or {}
    identity = source["result"].get("issuer_linkage") == "REGISTERED_CIK_AND_CURRENT_SEC_TICKER_MATCH"
    verified_attribution = source["result"].get("verification", {}).get("issuer_status") == "ISSUER_ATTRIBUTION_MATCH"
    # A registered name at the beginning of the headline binds the actor. Mere mention does not.
    name = re.sub(r",?\s+(?:Inc\.?|Ltd\.?|Co\.?|Holdings)(?:.*)?$", "", registry.get("name", ""), flags=re.I)
    starts_issuer = bool(name and re.match(re.escape(name) + r"\b", title, re.I))
    date = release_date(source)
    kind, role = "UNKNOWN", "UNKNOWN"
    if re.search(r"\b(?:reports?|announces?).*(?:quarter|fiscal).*results\b", title, re.I):
        kind, role = "EARNINGS", "REPORTING_COMPANY"
    elif re.search(r"\b(?:terminate|cancel)\w*.*(?:merger|acquisition|agreement)", title, re.I):
        kind, role = "DEAL_TERMINATION", "TRANSACTION_PARTY_UNRESOLVED"
    elif re.search(r"\b(?:to acquire|acquires|acquisition of)\b", title, re.I):
        kind, role = "M_AND_A", "ACQUIRER"
    elif re.search(r"\b(?:to be acquired|acquired by)\b", title, re.I):
        kind, role = "M_AND_A", "ACQUISITION_TARGET"
    elif re.search(r"\b(?:awarded|wins|secures)\b.*\bcontract\b", title, re.I):
        kind, role = "COMMERCIAL_CONTRACT", "CONTRACT_AWARDEE"
    elif re.search(r"\b(?:guidance|outlook)\b", title, re.I):
        kind, role = "GUIDANCE", "REPORTING_COMPANY"
    direct = starts_issuer and (identity or verified_attribution) and date is not None and kind != "UNKNOWN"
    title_row = next((p for p in source["parsed"]["paragraphs"] if p["text"] == title), None)
    return {"version": VERSION, "type": kind,
            "role": role if direct else "UNKNOWN", "relation": "DIRECT_COMPANY_DISCLOSURE_PROPOSAL" if direct else "UNRESOLVED",
            "evidence": ([anchor(title_row)] if title_row else []) + ([date["evidence"]] if date else []),
            "identity_linkage": "REGISTERED_SEC_CIK" if identity else "SOURCE_ATTRIBUTION" if verified_attribution else "UNVERIFIED",
            "freshness": freshness(source, as_of, calendar),
            "semantics": "RULE_CLASSIFIED_DISCLOSURE_NOT_VERIFIED_PRICE_CAUSE",
            "materiality": "NOT_ESTABLISHED", "theme_linkage": "NOT_INFERRED_FROM_ABSENCE_OF_DIRECT_NEWS",
            "eligible_for_rating": False}
