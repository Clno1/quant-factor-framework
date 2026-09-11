"""Locate model-selected context spans without inferring financial associations."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

VERSION = "ep-field-evidence-v1"
MAX_LOCATIONS = 64
FIELDS = {"SUBJECT": "subject_text", "PERIOD": "period_text",
          "CURRENCY": "unit_text", "SCALE": "unit_scale_text"}


class ContextEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["SUBJECT", "PERIOD", "CURRENCY", "SCALE"]
    text: str = Field(min_length=1, max_length=300)
    paragraph_id: str = Field(min_length=1, max_length=32)
    quote: str = Field(min_length=1, max_length=400)


def locate_context(claim, paragraphs):
    """Offsets are Python character indices in archived paragraphs, end exclusive."""
    errors, anchors = [], []
    for context in claim["context_evidence"]:
        paragraph = paragraphs.get(context["paragraph_id"], "")
        quote, text = context["quote"], context["text"]
        if not quote.strip() or quote not in paragraph or not text.strip() or text not in quote:
            errors.append("CONTEXT_QUOTE_NOT_IN_SOURCE")
            continue
        spans = []
        # Lookahead preserves overlapping matches; cap diagnostics for repetitive input.
        for match in re.finditer(r"(?=" + re.escape(quote) + r")", paragraph):
            for part in re.finditer(r"(?=" + re.escape(text) + r")", quote):
                spans.append({"quote_start": match.start(), "quote_end": match.start() + len(quote),
                              "text_start": match.start() + part.start(),
                              "text_end": match.start() + part.start() + len(text)})
                if len(spans) > MAX_LOCATIONS:
                    break
            if len(spans) > MAX_LOCATIONS:
                break
        anchors.append({**context, "occurrences": spans[:MAX_LOCATIONS],
                        "locations_truncated": len(spans) > MAX_LOCATIONS,
                        "location_status": "UNIQUE" if len(spans) == 1 else "AMBIGUOUS",
                        "association_verified": False})
    for role, field in FIELDS.items():
        value = claim[field]
        if value is not None and not any(a["role"] == role and a["text"] == value for a in anchors):
            errors.append(field.upper() + "_CONTEXT_ANCHOR_REQUIRED")
    return {"version": VERSION, "anchors": anchors, "errors": sorted(set(errors)),
            "offset_unit": "UNICODE_CODEPOINT_END_EXCLUSIVE",
            "association_verified": False, "table_column_alignment": "NOT_VERIFIED"}
