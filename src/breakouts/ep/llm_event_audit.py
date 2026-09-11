"""Offline scope and citation-coverage audit, separate from immutable v1 results.

Bindings are trusted local source-review annotations, never model output. Exact
span checks detect missing citations, not arbitrary semantic entailment.
"""
from copy import deepcopy
import re

from pydantic import Field, ValidationError

from .llm_contract import StrictModel
from .llm_event import EventNote, validate_event

VERSION = "ep-event-audit-v1"
CN = "零〇一二两三四五六七八九十百千万亿兆壹贰叁肆伍陆柒捌玖拾佰仟萬億兩"
NUMBER = rf"[{CN}]+(?:点[{CN}]+)?"
EN = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)"
QUANTITIES = {
    "DIGIT_OR_CURRENCY": re.compile(r"\d+(?:[.,，．]\d+)*|[$€£¥%％]"),
    "CHINESE_PERCENT": re.compile(rf"(?:百分之|千分之|万分之)\s*{NUMBER}"),
    "CHINESE_QUANTITY_OR_DATE": re.compile(
        rf"(?:第\s*)?{NUMBER}\s*(?:万|亿|萬|億|年|季度|月|日|天|美元|元|股|倍|成|处|个|段|家|项|笔|位|大业务板块)"),
    "CHINESE_SCALED_NUMBER": re.compile(rf"[零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖兩]+[十百千万亿兆拾佰仟萬億][{CN}]*"),
    "APPROXIMATE_QUANTITY": re.compile(r"数[十百千万亿]+|[几半][成倍]|一半|翻倍"),
    "ENGLISH_QUANTITY": re.compile(
        rf"\b{EN}(?:(?:[ -]+|\s+and\s+){EN})*\s+(?:percent|dollars?|shares?|spaces?|years?|quarters?|million|billion|thousand)\b", re.I),
    "ENGLISH_DATE": re.compile(r"\b(?:first|second|third|fourth)\s+quarter\b", re.I),
}


def quantity_expressions(text):
    """Return exact original offsets; findings flag scope, not numerical falsehood."""
    found = []
    for kind, pattern in QUANTITIES.items():
        for match in pattern.finditer(text):
            found.append({"kind": kind, "start": match.start(), "end": match.end(), "text": match.group()})
    return sorted(found, key=lambda item: (item["start"], item["end"], item["kind"]))


class EvidenceBinding(StrictModel):
    paragraph_id: str
    quote: str = Field(min_length=1)


class ClaimBinding(StrictModel):
    text: str = Field(min_length=1)
    evidence: list[EvidenceBinding] = Field(min_length=1)


class NoteBinding(StrictModel):
    index: int = Field(ge=0)
    original_note: EventNote
    claims: list[ClaimBinding] = Field(min_length=1)


class ReviewBindings(StrictModel):
    packet_hash: str
    origin: str
    notes: list[NoteBinding]


def _unique_span(text, excerpt):
    start = text.find(excerpt)
    if start < 0 or text.find(excerpt, start + 1) >= 0:
        raise ValueError("EVENT_REVIEW_SPAN_NOT_UNIQUE")
    return {"start": start, "end": start + len(excerpt), "text": excerpt}


def _coverage(text, claims):
    cursor, uncovered = 0, []
    for span in sorted(claims, key=lambda item: item["start"]):
        if span["start"] < cursor:
            raise ValueError("EVENT_REVIEW_CLAIMS_OVERLAP")
        gap = text[cursor:span["start"]]
        if any(char.isalnum() for char in gap):
            uncovered.append({"start": cursor, "end": span["start"], "text": gap})
        cursor = span["end"]
    if any(char.isalnum() for char in text[cursor:]):
        uncovered.append({"start": cursor, "end": len(text), "text": text[cursor:]})
    return uncovered


def audit_event(packet, response, bindings=None):
    """No transport, key, database, budget or delivery access; input is never changed."""
    previous = validate_event(packet, response)
    blocks = {p["paragraph_id"]: p["text"] for p in packet["request"]["untrusted_blocks"]}
    reviews = {}
    if bindings is not None:
        try:
            reviewed = ReviewBindings.model_validate(bindings).model_dump()
        except ValidationError:
            raise ValueError("INVALID_EVENT_REVIEW_BINDINGS") from None
        if reviewed["origin"] != "LOCAL_SOURCE_REVIEW" or reviewed["packet_hash"] != packet["packet_hash"]:
            raise ValueError("EVENT_REVIEW_PACKET_MISMATCH")
        for note in reviewed["notes"]:
            index = note["index"]
            if index in reviews or index >= len(response["notes"]) or note["original_note"] != response["notes"][index]:
                raise ValueError("EVENT_REVIEW_NOTE_MISMATCH")
            reviews[index] = note
    old_rejections = {n["index"]: n["reasons"] for n in previous["rejected"]}
    items = []
    for index, note in enumerate(response["notes"]):
        claims = []
        for claim in reviews.get(index, {}).get("claims", []):
            span = _unique_span(note["text"], claim["text"])
            evidence = []
            for entry in claim["evidence"]:
                pid = entry["paragraph_id"]
                if pid not in blocks:
                    raise ValueError("EVENT_REVIEW_UNKNOWN_PARAGRAPH")
                evidence.append({"paragraph_id": pid, **_unique_span(blocks[pid], entry["quote"])})
            required = {item["paragraph_id"] for item in evidence}
            missing = sorted(required - set(note["paragraph_ids"]))
            claims.append({**span, "evidence": evidence, "missing_paragraph_ids": missing,
                           "status": "MISSING_CITATION" if missing else "BOUND_EVIDENCE_CITED"})
        uncovered = _coverage(note["text"], claims)
        quantities = quantity_expressions(note["text"])
        reasons = list(old_rejections.get(index, []))
        if quantities:
            reasons.append("QUANTITY_EXPRESSION_OUTSIDE_EVENT_SCOPE")
        if any(claim["missing_paragraph_ids"] for claim in claims):
            reasons.append("CLAIM_EVIDENCE_NOT_CITED")
        unresolved = ["SEMANTIC_SUPPORT_NOT_AUTOMATICALLY_VERIFIED"]
        if uncovered:
            unresolved.append("UNREVIEWED_CLAIM_TEXT")
        items.append({"index": index, "note": deepcopy(note), "status": "BLOCKED" if reasons else "REVIEW_REQUIRED",
                      "reasons": sorted(set(reasons)), "quantity_expressions": quantities,
                      "reviewed_claims": claims, "unreviewed_spans": uncovered,
                      "claim_inventory_complete": not uncovered, "unresolved": unresolved,
                      "semantic_support_verified": False})
    return {"version": VERSION, "packet_hash": packet["packet_hash"], "items": items,
            "previous_validation": previous, "annotation_origin": "LOCAL_SOURCE_REVIEW" if bindings else None,
            "semantic_support_verified": False, "eligible_for_rating": False,
            "delivery": "DISABLED_SHADOW_ONLY", "external_requests": 0}
