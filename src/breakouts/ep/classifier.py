"""Conservative headline hints, never a substitute for verified issuer evidence."""
from __future__ import annotations

import re
from typing import Any


def classify(document: dict[str, Any]) -> dict[str, Any]:
    kind = "UNKNOWN"
    reason = "FULLTEXT_AND_ISSUER_ROLE_UNVERIFIED"
    if document["kind"] == "CALENDAR":
        kind, reason = "EARNINGS_CALENDAR", "PUBLICATION_TIME_AND_EPS_BASIS_UNVERIFIED"
    else:
        title = document["payload"]["title"].casefold()
        if re.search(r"class action|shareholder alert|lead plaintiff|investor deadline", title):
            kind, reason = "LEGAL_NOTICE", "LEGAL_NOTICE_NOT_OPERATING_CATALYST"
        elif re.search(r"(?:terminat\w*|cancel\w*).*(?:merger|acquisition|agreement)", title):
            kind = "DEAL_TERMINATION"
        elif re.search(r"(?:reports?|announces?).*(?:quarter|fiscal).*results|results.*quarter", title):
            kind = "EARNINGS"
        elif re.search(r"acquires?|acquisition|merger", title):
            kind = "M_AND_A"
        elif re.search(r"contract|selected for|purchase order", title):
            kind = "COMMERCIAL_CONTRACT"
        elif re.search(r"guidance|outlook", title):
            kind = "GUIDANCE"
    return {"document_id": document["document_id"], "revision_id": document["revision_id"],
            "event_type_hint": kind, "relation": "UNVERIFIED", "confidence": "HEADLINE_ONLY",
            "reason": reason, "published_at": document["published_at"],
            "first_seen_at": document["first_seen_at"], "evidence": document["payload"]}
