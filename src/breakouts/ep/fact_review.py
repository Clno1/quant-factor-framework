"""Offline evidence dossiers and strict, append-only human review contracts."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import re

from .models import digest, timestamp


VERSION = "ep-fact-review-v1"
TOPICS = {
    "FINANCIAL_BASIS": r"\b(?:GAAP|adjusted|diluted|basic|per share|EPS)\b",
    "OPERATING_METRIC": r"\b(?:revenue|ARR|cRPO|backlog|book.to.bill|same.store|GMV|retention)\b",
    "GUIDANCE_OR_ESTIMATE": r"\b(?:guidance|outlook|expects?|forecast|consensus|estimat\w*)\b",
    "ONE_TIME_ITEM": r"\b(?:one.time|tax benefit|refund|repurchase|buyback|non.recurring)\b",
    "EVENT_ROLE": r"\b(?:acquir\w*|acquisition|merger|contract|customer|supplier|partnership)\b",
    "PERIOD_OR_TIME": r"\b(?:quarter|fiscal|year ended|announced|announces)\b",
}


def review_template(source: dict, *, max_paragraphs: int = 40) -> dict:
    if type(max_paragraphs) is not int or not 1 <= max_paragraphs <= 200:
        raise ValueError("max_paragraphs must be between 1 and 200")
    parsed = source.get("parsed") or {}
    verification = source["result"].get("verification", {})
    blockers = []
    if verification.get("status") != "DOCUMENT_MATCHED":
        blockers.append("DOCUMENT_ALIGNMENT_REQUIRED")
    if verification.get("issuer_status") != "ISSUER_ATTRIBUTION_MATCH":
        blockers.append("ISSUER_IDENTITY_REVIEW_REQUIRED")
    hits, counts = [], Counter()
    for paragraph in parsed.get("paragraphs", []):
        topics = [key for key, pattern in TOPICS.items() if re.search(pattern, paragraph["text"], re.I)]
        if topics:
            counts.update(topics)
            hits.append({**paragraph, "topics": topics, "semantics": "NAVIGATION_HINT_NOT_EXTRACTED_FACT"})
    # Round-robin topics prevents long EPS tables from hiding contract or one-time-item evidence.
    selected, seen = [], set()
    queues = {topic: iter([row for row in hits if topic in row["topics"]]) for topic in TOPICS}
    while len(selected) < min(max_paragraphs, len(hits)):
        progress = False
        for queue in queues.values():
            row = next((row for row in queue if row["id"] not in seen), None)
            if row is not None and len(selected) < max_paragraphs:
                selected.append(row)
                seen.add(row["id"])
                progress = True
        if not progress:
            break
    return {
        "schema_version": VERSION, "source_id": source["source_id"], "document_id": source["document_id"],
        "text_revision": parsed.get("text_revision"), "ticker": source.get("ticker"),
        "title": parsed.get("title"), "source_url": source["result"].get("final_url"),
        "source_received_at": source["result"].get("retrieved_at", source["observed_at"]),
        "provider_publication_at": source.get("published_at"),
        "publication_semantics": "PROVIDER_TIME_NOT_ORIGINAL_PUBLICATION_PROOF",
        "status": "REVIEW_REQUIRED", "blockers": blockers,
        "issuer_attributions": parsed.get("issuer_attributions", []),
        "evidence_index": selected, "topic_counts": dict(counts),
        "matching_paragraphs": len(hits), "omitted_matching_paragraphs": len(hits) - len(selected),
        "total_paragraphs": len(parsed.get("paragraphs", [])),
        "review_input": {"schema_version": VERSION, "source_id": source["source_id"],
                         "document_id": source["document_id"], "text_revision": parsed.get("text_revision"),
                         "role": None, "publication": None, "facts": [], "note": ""},
        "limitations": ["KEYWORDS_ONLY_LOCATE_EVIDENCE", "TABLE_HEADERS_NOT_RECONSTRUCTED",
                        "NO_AUTOMATIC_EVENT_FRESHNESS_OR_CAUSALITY", "NO_ESTIMATE_ASOF_PROOF"],
        "eligible_for_rating": False, "delivery": "DISABLED_SHADOW_ONLY",
    }


def _text(value, label, *, maximum=5000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Invalid {label}")
    return value


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"Invalid {label}")
    return value


def _evidence(anchor, paragraphs):
    if not isinstance(anchor, dict):
        raise ValueError("Evidence anchor required")
    paragraph_id = _text(anchor.get("paragraph_id"), "paragraph_id", maximum=32)
    quote = _text(anchor.get("quote"), "quote")
    if paragraph_id not in paragraphs or quote not in paragraphs[paragraph_id]:
        raise ValueError("Evidence quote does not match the source paragraph")
    return {"paragraph_id": paragraph_id, "quote": quote}


def _quoted(value, quote, label):
    value = _text(value, label, maximum=500)
    if value in {"$", "%"}:
        matches = value in quote
    else:
        matches = re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", quote, re.I)
    if not matches:
        raise ValueError(f"{label} is not quoted")
    return value


def validate_review(source: dict, payload: dict, reviewer: str, now: datetime) -> dict:
    """Validate evidence containment, never claim that human-selected semantics are proven."""
    reviewer = _text(reviewer, "reviewer", maximum=100)
    parsed = source.get("parsed") or {}
    if (parsed.get("status") != "EXTRACTED" or
            source["result"].get("verification", {}).get("status") != "DOCUMENT_MATCHED"):
        raise ValueError("Review requires an extracted, aligned document")
    if not isinstance(payload, dict) or payload.get("schema_version") != VERSION:
        raise ValueError("Invalid review schema")
    for key, expected in (("source_id", source["source_id"]), ("document_id", source["document_id"]),
                          ("text_revision", parsed["text_revision"])):
        if payload.get(key) != expected:
            raise ValueError("Review refers to a different source or text version")
    paragraphs = {row["id"]: row["text"] for row in parsed["paragraphs"]}
    role = None
    if payload.get("role") is not None:
        value = payload["role"]
        if not isinstance(value, dict):
            raise ValueError("Invalid role")
        anchor = _evidence(value.get("evidence"), paragraphs)
        role = {"issuer_name": _quoted(value.get("issuer_name"), anchor["quote"], "issuer_name"),
                "kind": _choice(value.get("kind"), {"REPORTING_COMPANY", "ACQUIRER", "ACQUISITION_TARGET",
                    "CONTRACT_AWARDEE", "CUSTOMER", "PARTNER", "MENTION_ONLY", "UNKNOWN"}, "role kind"),
                "evidence": anchor, "semantics": "HUMAN_ASSERTION_NOT_AUTOMATIC_ROLE_PROOF"}
    publication = None
    if payload.get("publication") is not None:
        value = payload["publication"]
        if not isinstance(value, dict):
            raise ValueError("Invalid publication")
        anchor = _evidence(value.get("evidence"), paragraphs)
        publication = {"date_text": _quoted(value.get("date_text"), anchor["quote"], "date_text"),
                       "kind": _choice(value.get("kind"), {"ANNOUNCEMENT_DATE", "FISCAL_PERIOD_DATE",
                           "FUTURE_EVENT_DATE", "UNKNOWN"}, "publication kind"), "evidence": anchor,
                       "exact_release_time_verified": False}
    rows = payload.get("facts")
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("facts must be a list of at most 100 items")
    facts, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid fact row")
        anchor = _evidence(row.get("evidence"), paragraphs)
        fact = {"metric": _choice(row.get("metric"), {"REVENUE", "EPS", "NET_INCOME", "ARR", "CRPO",
                "BACKLOG", "BOOK_TO_BILL", "SAME_STORE_SALES", "GMV", "OTHER"}, "metric"),
                "metric_text": _quoted(row.get("metric_text"), anchor["quote"], "metric_text"),
                "value_text": _quoted(row.get("value_text"), anchor["quote"], "value_text"),
                "measure": _choice(row.get("measure"), {"LEVEL", "GROWTH_RATE", "MARGIN", "RATIO", "UNKNOWN"}, "measure"),
                "basis": _choice(row.get("basis"), {"GAAP", "NON_GAAP", "NOT_APPLICABLE", "UNKNOWN"}, "basis"),
                "share_basis": _choice(row.get("share_basis"), {"BASIC", "DILUTED", "NOT_APPLICABLE", "UNKNOWN"}, "share basis"),
                "value_kind": _choice(row.get("value_kind"), {"ACTUAL", "COMPANY_GUIDANCE", "CONSENSUS", "UNKNOWN"}, "value kind"),
                "evidence": anchor}
        for field in ("period_text", "unit_text", "basis_text", "share_basis_text", "value_kind_text"):
            fact[field] = _quoted(row[field], anchor["quote"], field) if row.get(field) is not None else None
        if fact["basis_text"]:
            basis = fact["basis_text"].upper().replace(" ", "-")
            if fact["basis"] == "GAAP" and (basis != "GAAP" or not re.search(
                    r"(?<![\w-])(?<!non )GAAP\b", anchor["quote"], re.I)):
                raise ValueError("GAAP label contradicts quoted basis")
            if fact["basis"] == "NON_GAAP" and basis != "NON-GAAP":
                raise ValueError("NON_GAAP requires explicit non-GAAP text, not inferred adjusted earnings")
        if fact["share_basis_text"] and fact["share_basis"] in {"BASIC", "DILUTED"}:
            if fact["share_basis_text"].upper() != fact["share_basis"]:
                raise ValueError("Share basis label contradicts quoted text")
        missing = [field for field in ("period_text", "unit_text", "value_kind_text") if not fact[field]]
        missing.extend(field for field in ("measure", "basis", "value_kind") if fact[field] == "UNKNOWN")
        if fact["basis"] in {"GAAP", "NON_GAAP"} and not fact["basis_text"]:
            missing.append("basis_text")
        if fact["share_basis"] in {"BASIC", "DILUTED"} and not fact["share_basis_text"]:
            missing.append("share_basis_text")
        if fact["metric"] == "EPS" and (fact["basis"] in {"UNKNOWN", "NOT_APPLICABLE"} or
                                         fact["share_basis"] in {"UNKNOWN", "NOT_APPLICABLE"}):
            missing.append("EPS_BASIS_UNRESOLVED")
        if fact["value_kind"] == "CONSENSUS":
            missing.append("CONSENSUS_ASOF_NOT_PROVEN")
        fact["missing_context"] = missing
        fact["comparison_status"] = "DISABLED_REVIEW_ONLY"
        fact["semantics"] = "HUMAN_LABELS_WITH_EXACT_QUOTES_NOT_VERIFIED_FINANCIAL_FACTS"
        identity = digest(fact)
        if identity not in seen:
            seen.add(identity)
            facts.append(fact)
    if not role and not publication and not facts:
        raise ValueError("Empty review cannot be submitted")
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError("Invalid review note")
    return {"schema_version": VERSION, "source_id": source["source_id"], "document_id": source["document_id"],
            "text_revision": parsed["text_revision"], "reviewer": reviewer, "recorded_at": timestamp(now),
            "role": role, "publication": publication, "facts": facts, "note": note,
            "status": "HUMAN_REVIEW_RECORDED", "issuer_status": source["result"]["verification"].get("issuer_status"),
            "financial_semantics_verified": False, "historical_availability_verified": False,
            "eligible_for_rating": False, "delivery": "DISABLED_SHADOW_ONLY"}
