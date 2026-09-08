"""Offline EP source analysis; independent of candidate admission and price triggers."""
from __future__ import annotations

from datetime import datetime, timezone

from .catalyst import classify_catalyst
from .dossier import candidate_dossier
from .facts import extract_financial_proposals
from .models import digest, timestamp

VERSION = "ep-source-analysis-v1"


def analyze_candidate(store, symbol, *, run_id=None, as_of=None, calendar=None):
    as_of = as_of or datetime.now(timezone.utc)
    timestamp(as_of)
    dossier = candidate_dossier(store, symbol, run_id=run_id, as_of=as_of)
    result = {"version": VERSION, "ticker": dossier["ticker"], "run_id": dossier["run_id"],
              "evidence_as_of": timestamp(as_of), "collection_scope": dossier["collection_scope"],
              "status": "SOURCE_GAPS_REMAIN", "sources": [],
              "latest_source_attempts": dossier["latest_source_attempts"],
              "missing_checks": dossier["missing_checks"], "grade": None, "eligible_for_rating": False,
              "delivery": "DISABLED_SHADOW_ONLY", "llm": "NOT_USED_OFFLINE_RULES",
              "financial_semantics_verified": False, "historical_realtime_availability_verified": False}
    for entry in dossier["sources"]:
        source = store.source_detail(entry["source_id"], as_of=as_of)
        facts = extract_financial_proposals(source)
        catalyst = classify_catalyst(source, as_of, calendar)
        result["sources"].append({"source_id": source["source_id"], "document_id": source["document_id"],
            "text_revision": source["parsed"]["text_revision"], "source_url": entry["url"],
            "source_received_at": entry["received_at"], "issuer_review_blockers": entry["review_blockers"],
            "financials": facts, "catalyst": catalyst,
            "human_review_ids": [review["review_id"] for review in source["human_reviews"]]})
    if result["sources"]:
        result["status"] = "PROPOSALS_READY_FOR_REVIEW"
    result["summary"] = {"aligned_source_count": len(result["sources"]),
        "financial_proposal_count": sum(len(s["financials"]["proposals"]) for s in result["sources"]),
        "special_item_paragraph_count": sum(len(s["financials"]["special_items"]) for s in result["sources"]),
        "coverage_gap_count": sum(len(s["financials"]["coverage_gaps"]) for s in result["sources"]),
        "meaning": "PARSED_DISCLOSURES_NOT_CONFIRMED_EP_SIGNALS"}
    result["analysis_id"] = digest(result)
    return result
