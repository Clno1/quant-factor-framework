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
        elif re.search(r'\b(?:acquires?|buys?|purchases?|sells?|sold)\s+[\d,.]+\s+shares\s+(?:of|in)\b', title):
            kind, reason = 'OWNERSHIP_UPDATE', 'SHAREHOLDER_POSITION_NOT_COMPANY_ACQUISITION'
        elif re.search(r'\b(?:to report|sets? the date|earnings ahead|earnings preview|before earnings|ahead of (?:q[1-4] )?earnings|expected to .*earnings)\b', title):
            kind, reason = 'EARNINGS_PREVIEW', 'SCHEDULE_OR_PREVIEW_NOT_RELEASED_RESULTS'
        elif re.search(r"(?:terminat\w*|cancel\w*).*(?:merger|acquisition|agreement)", title):
            kind = "DEAL_TERMINATION"
        elif re.search(r"(?:reports?|announces?).*(?:quarter|fiscal).*results|results.*quarter|\bq[1-4]\b.*\b(?:earnings|results)\b|\bearnings (?:beat|miss|call)\b", title):
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


def routing_hint(event):
    """Re-evaluate old queued headlines without rewriting their original evidence."""
    if event.get('event_type_hint') == 'EARNINGS_CALENDAR':
        return 'EARNINGS_CALENDAR'
    result = classify({'kind': 'ARTICLE', 'payload': event['evidence'],
        'document_id': event['document_id'], 'revision_id': event['revision_id'],
        'published_at': event.get('published_at'), 'first_seen_at': event.get('first_seen_at')})
    hint = result['event_type_hint']
    return hint if hint != 'UNKNOWN' else event.get('event_type_hint', 'UNKNOWN')
