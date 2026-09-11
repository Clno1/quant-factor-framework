"""Conservative event grouping hints, not verification of news or financial claims."""
import re
from datetime import datetime

from .catalyst import event_window
from .models import digest


def quarter(text):
    normalized = text.casefold()
    for word, number in [('first', '1'), ('second', '2'), ('third', '3'), ('fourth', '4')]:
        normalized = re.sub(r'\b' + word + r'[ -]quarter\b', 'q' + number, normalized)
    values = set(re.findall(r'\bq([1-4])\b', normalized))
    return next(iter(values)) if len(values) == 1 else None


def fiscal_year(text):
    values = set(re.findall(r'\b(?:fy\s*|fiscal(?:\s+year)?\s+)(20\d{2}|\d{2})\b', text, re.I))
    values = {v if len(v) == 4 else '20' + v for v in values}
    return next(iter(values)) if len(values) == 1 else None


def earnings_period(title, introduction=''):
    title_quarter, intro_quarter = quarter(title), quarter(introduction)
    if title_quarter and intro_quarter and title_quarter != intro_quarter:
        return None
    q = title_quarter or intro_quarter
    y = fiscal_year(title) or fiscal_year(introduction)
    # Official headings sometimes omit "fiscal" (RH). Do not use arbitrary
    # datelines or table years to infer the period.
    if not y and q:
        match = re.search(r'\b(?:quarter|q[1-4])\s+(20\d{2})\s+(?:financial\s+)?results', title, re.I)
        y = match[1] if match else None
    return (y, q) if y and q else None


def event_descriptor(symbol, event):
    title = event['evidence'].get('title', '')
    intro = event['evidence'].get('text', '')[:1500]
    hint = event.get('event_type_hint', 'UNKNOWN')
    period = earnings_period(title, intro)
    earnings = bool(re.search(r'earnings|financial results|quarter.*results|results.*quarter', title, re.I)
                    or re.search(r'reported .*quarter.*results', intro, re.I))
    session = event_window(datetime.fromisoformat(event['published_at']))['session']
    if period and earnings and hint not in {'M_AND_A', 'DEAL_TERMINATION', 'COMMERCIAL_CONTRACT'}:
        identity = [symbol, 'EARNINGS', session, *period]
        scope = 'ISSUER_FISCAL_PERIOD_HINT'
        hint = 'EARNINGS'
    else:
        # No speculative merger of different contracts, targets or unknown
        # periods. Exact headline syndication can still share one task.
        headline = ' '.join(re.findall(r'[a-z0-9]+', title.casefold()))
        identity = [symbol, hint, session, headline or event['document_id']]
        scope = 'ISSUER_SESSION_EXACT_HEADLINE_HINT'
    return {'event_id': digest(['ep-event-v1', identity]), 'event_type': hint,
            'period': list(period) if period and earnings else None, 'session': session,
            'grouping_scope': scope, 'event_verified': False}
