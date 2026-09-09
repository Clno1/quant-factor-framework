"""Read-only candidate evidence reports; facts remain review assertions, never signals."""
from __future__ import annotations

from .fact_review import review_template


def candidate_dossier(store, symbol, *, run_id=None, as_of=None) -> dict:
    explanation = store.explain(symbol, run_id=run_id, as_of=as_of)
    candidate = explanation["candidate"]
    result = {"version": "ep-dossier-v1", "ticker": explanation["ticker"], "run_id": explanation["run_id"],
              "as_of": explanation["as_of"], "status": explanation["reason"],
              "collection_scope": explanation["scope"], "events": [], "sources": [],
              "latest_source_attempts": explanation["source_enrichment"]["sources"],
              "facts": [], "grade": None, "eligible_for_rating": False,
              "delivery": "DISABLED_SHADOW_ONLY", "notification_audience": "OWNER_ONLY_USER_DECLARED",
              "llm": "NOT_CONFIGURED", "missing_checks": []}
    if candidate is None:
        result["missing_checks"] = [explanation["reason"]]
        return result
    result["collection_status"] = candidate["status"]
    result["identity_at_collection"] = candidate["identity"]
    for event in candidate["events"]:
        result["events"].append({"document_id": event["document_id"], "revision_id": event["revision_id"],
            "title": event["evidence"].get("title"), "source_url": event["evidence"].get("url"),
            "provider_published_at": event["published_at"], "first_seen_at": event["first_seen_at"],
            "event_type_hint": event["event_type_hint"], "relation": event["relation"],
            "semantics": "UNVERIFIED_EVENT_HINT_NOT_CONFIRMED_CATALYST"})
    current = {(e["document_id"], e["revision_id"]) for e in candidate["events"]}
    for source in store.aligned_sources(explanation["run_id"], explanation["ticker"], as_of=as_of):
        if (source["document_id"], source["revision_id"]) not in current:
            continue
        template = review_template(source, max_paragraphs=6)
        result["sources"].append({"source_id": source["source_id"], "document_id": source["document_id"],
            "text_revision": template["text_revision"], "url": template["source_url"], "title": template["title"],
            "received_at": template["source_received_at"], "review_blockers": template["blockers"],
            "topic_counts": template["topic_counts"], "evidence_locations": [
                {"paragraph_id": p["id"], "topics": p["topics"]} for p in template["evidence_index"]],
            "text_access": "Use source or review-template with source_id; no full text in this report",
            "provenance": source["result"].get("discovery_steps", []),
            "financial_facts_verified": False})
        for review in source["human_reviews"]:
            result["facts"].extend({"review_id": review["review_id"], "source_id": source["source_id"], "fact": fact,
                                    "semantics": "HUMAN_REVIEW_ASSERTION_NOT_AUTOMATIC_FACT_PROOF"} for fact in review["facts"])
    result["missing_checks"] = ["CATALYST_ROLE_AND_FRESHNESS_REVIEW", "FINANCIAL_BASIS_AND_ESTIMATE_ASOF",
                                "PREMARKET_VOLUME_CONTRACT", "OPENING_PRICE_CONFIRMATION"]
    if not result["sources"]:
        result["missing_checks"].insert(0, "ALIGNED_ORIGINAL_TEXT_REQUIRED")
    if not result["facts"]:
        result["missing_checks"].insert(0, "NO_REVIEWED_FINANCIAL_FACTS")
    result["status"] = "EVIDENCE_READY_FOR_REVIEW" if result["sources"] else "SOURCE_GAPS_REMAIN"
    return result
