"""Offline relationship checks layered over immutable span-selection results.

These bounded prose checks can disprove associations, not certify financial facts.
No provider, journal mutation, rating or delivery dependencies belong here.
"""
from __future__ import annotations

import re

from .llm_span_selection import validate_selection

VERSION = "ep-association-audit-v1"
COMPARISON = re.compile(r"\bcompared\s+(?:with|to)\b|\bversus\b|\bvs\.?\s|\bwhereas\b", re.I)
NEXT_AMOUNT = re.compile(r"\band\s+(?=[$\u20ac\u00a3]\s*\(?\d)", re.I)
SCALE = re.compile(r"(?:thousand|million|billion)s?", re.I)


def _zones(text):
    # Split explicit comparison lists only. Do not infer arbitrary sentence/table grammar.
    comparisons = [m.start() for m in COMPARISON.finditer(text)]
    continuations = [m.start() for m in NEXT_AMOUNT.finditer(text)
                     if any(start < m.start() for start in comparisons)]
    boundaries = sorted(set(comparisons + continuations))
    points = [0, *[b for b in boundaries if b > 0], len(text)]
    return list(zip(points, points[1:]))


def _zone(span, blocks):
    for start, end in _zones(blocks[span["paragraph_id"]]["text"]):
        if start <= span["start"] and span["end"] <= end:
            return (start, end)
    return None


def _check(claim, trace, blocks):
    contradictions, unresolved, locations = [], [], {}
    value = trace["value_span"]
    value_zone = _zone(value, blocks)

    def relation(role, span):
        if span is None:
            unresolved.append(role + "_EVIDENCE_MISSING")
            locations.setdefault(role, []).append("MISSING")
            return False
        if span["paragraph_id"] != value["paragraph_id"]:
            unresolved.append(role + "_CROSS_PARAGRAPH_ASSOCIATION_UNRESOLVED")
            locations.setdefault(role, []).append("CROSS_PARAGRAPH")
            return False
        zone = _zone(span, blocks)
        if zone is not None and value_zone is not None and zone != value_zone:
            # Periods in explicit comparison branches belong to different observations.
            # Issuer, basis and units may instead be inherited across a comparison list.
            if role == "PERIOD":
                contradictions.append("PERIOD_CROSSES_COMPARISON_BRANCH")
            else:
                unresolved.append(role + "_CROSS_BRANCH_ASSOCIATION_UNRESOLVED")
            locations.setdefault(role, []).append("DIFFERENT_COMPARISON_BRANCH")
            return False
        if zone is None or value_zone is None:
            unresolved.append(role + "_BRANCH_ASSOCIATION_UNRESOLVED")
            locations.setdefault(role, []).append("SPANS_BRANCH_BOUNDARY")
            return False
        locations.setdefault(role, []).append("SAME_COMPARISON_BRANCH")
        return True

    periods = trace["period_spans"]
    if not periods:
        relation("PERIOD", None)
    for period in periods:
        relation("PERIOD", period)
    # Even within one branch there can be multiple values, subjects, or table columns.
    unresolved.append("PERIOD_VALUE_SEMANTICS_NOT_CERTIFIED")
    relation("SUBJECT", trace["subject_span"])
    unresolved.append("SUBJECT_IDENTITY_AND_SCOPE_NOT_CERTIFIED")

    basis = trace["basis_span"]
    if claim["basis"] in {"GAAP", "NON_GAAP"}:
        relation("BASIS", basis)
        allowed = {"GAAP": r"GAAP|generally accepted accounting principles",
                   "NON_GAAP": r"non[\s\-\u2011\u2013]*GAAP"}
        if basis is None or not re.fullmatch(allowed[claim["basis"]], basis["text"].strip(), re.I):
            contradictions.append("ACCOUNTING_BASIS_EVIDENCE_MISMATCH")
        elif claim["basis"] == "GAAP":
            prefix = blocks[basis["paragraph_id"]]["text"][:basis["start"]]
            if re.search(r"non[\s\-\u2011\u2013]*$", prefix, re.I):
                contradictions.append("GAAP_SELECTED_INSIDE_NON_GAAP")
    else:
        unresolved.append("ACCOUNTING_BASIS_NOT_CERTIFIED")

    unit = trace["unit_span"]
    relation("UNIT", unit)
    if unit and not re.fullmatch(r"[$\u20ac\u00a3%]|USD|EUR|GBP|dollars?|percent", unit["text"].strip(), re.I):
        contradictions.append("UNIT_SPAN_IS_NOT_UNIT_ONLY")
    scale = trace["unit_scale_span"]
    if scale:
        relation("SCALE", scale)
        if not SCALE.fullmatch(scale["text"].strip()):
            contradictions.append("SCALE_SPAN_IS_NOT_SCALE_ONLY")

    share = trace["share_basis_span"]
    if claim["share_basis"] in {"DILUTED", "BASIC"}:
        relation("SHARE_BASIS", share)
        if share is None or not re.search(r"\b" + claim["share_basis"] + r"\b", share["text"], re.I):
            contradictions.append("SHARE_BASIS_EVIDENCE_MISMATCH")
        text = blocks[value["paragraph_id"]]["text"]
        # Inspect the selected amount's immediate suffix, never another nearby amount's scale.
        suffix = text[value["end"]:value["end"] + 24]
        amount_has_scale = re.search(r"\b(?:thousand|million|billion)s?\b", value["text"], re.I)
        amount_has_scale = amount_has_scale or re.match(r"\s*(?:thousand|million|billion)s?\b", suffix, re.I)
        if amount_has_scale:
            unresolved.append("SCALED_PER_SHARE_AMOUNT_REQUIRES_REVIEW")
            if not re.search(r"\bper\s+(?:\w+\s+)?share\b", value["text"] + suffix, re.I):
                contradictions.append("SCALED_TOTAL_ASSIGNED_PER_SHARE_BASIS")

    return {"contradictions": sorted(set(contradictions)), "unresolved": sorted(set(unresolved)),
            "field_locations": locations, "value_branch": value_zone,
            "financial_semantics_verified": False}


def audit_associations(packet, response):
    """Revalidate IDs first, then separately audit relations; never change old results."""
    grounded = validate_selection(packet, response)
    blocks = {b["paragraph_id"]: b for b in packet["request"]["untrusted_blocks"]}
    items = []
    for old_status in ("accepted", "rejected"):
        for item in grounded[old_status]:
            ordinal = item.get("selection_index", item.get("index"))
            claim = item.get("materialized_claim", item)
            trace = item.get("selected_spans")
            checks = _check(claim, trace, blocks) if trace else {
                "contradictions": [], "unresolved": ["SPAN_MATERIALIZATION_FAILED"],
                "field_locations": {}, "financial_semantics_verified": False}
            blocked = old_status == "rejected" or bool(checks["contradictions"])
            items.append({"selection_index": ordinal, "text_validation_status": old_status.upper(),
                          "text_rejection_reasons": item.get("reasons", []),
                          "status": "BLOCKED" if blocked else "REVIEW_REQUIRED",
                          "value_text": claim.get("value_text"), "checks": checks})
    items.sort(key=lambda item: item["selection_index"])
    return {"version": VERSION, "request_id": packet["request"]["request_id"],
            "packet_hash": packet["packet_hash"], "items": items,
            "text_validation": grounded, "financial_semantics_verified": False,
            "eligible_for_rating": False, "delivery": "DISABLED_SHADOW_ONLY", "external_requests": 0}
