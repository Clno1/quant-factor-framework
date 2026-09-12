"""Observed stage clocks; no inferred arrival times or cross-event completion joins."""
from datetime import date, datetime
import json

from .models import digest, encode, ticker, timestamp


def elapsed(end, start):
    if not end or not start:
        return None
    value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    return value if value >= 0 else None


def record(queue, symbol, session, kind, key, now, payload):
    queue._writable()
    with queue.connection() as db:
        db.execute('INSERT OR IGNORE INTO latency_events VALUES(?,?,?,?,?,?)',
                   (digest([symbol, session, kind, key]), ticker(symbol), session, kind, timestamp(now), encode(payload)))


def report(queue, symbol, session, as_of):
    if date.fromisoformat(session).isoformat() != session:
        raise ValueError('CANONICAL_SESSION_DATE_REQUIRED')
    symbol, cutoff = ticker(symbol), timestamp(as_of)
    with queue.connection() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='latency_events'").fetchone()
        rows = [dict(r) for r in db.execute('''SELECT * FROM latency_events WHERE ticker=? AND session=?
            AND observed_at<=? ORDER BY observed_at,id''', (symbol, session, cutoff))] if exists else []
        prices = [r for r in rows if r['kind'] == 'PRICE_DISCOVERED']
        first = prices[0] if prices else None
        searches = [dict(r) for r in db.execute('''SELECT t.observed_at,t.state,t.reason,j.job_id
            FROM transitions t JOIN jobs j ON j.job_id=t.job_id
            WHERE j.ticker=? AND j.stage='NEWS' AND json_extract(j.payload,'$.session')=?
            AND t.observed_at<=? ORDER BY t.observed_at,t.id''', (symbol, session, cutoff))]
        events = []
        for row in rows:
            if row['kind'] != 'SOURCE_MATCHED':
                continue
            payload = json.loads(row['payload'])
            analysis = db.execute('SELECT created_at FROM jobs WHERE job_id=? AND created_at<=?',
                                  (payload['analysis_job_id'], cutoff)).fetchone()
            transitions = [dict(t) for t in db.execute('''SELECT observed_at,state,reason FROM transitions
                WHERE job_id=? AND observed_at<=? ORDER BY observed_at,id''', (payload['analysis_job_id'], cutoff))]
            started = next((t['observed_at'] for t in transitions if t['state'] == 'RUNNING'), None)
            completed = next((t['observed_at'] for t in transitions if t['state'] == 'COMPLETE'), None)
            news = payload.get('news_first_seen_at')
            events.append({**payload, 'source_matched_at': row['observed_at'],
                'analysis_queued_at': analysis[0] if analysis else None,
                'analysis_started_at': started, 'analysis_completed_at': completed,
                'analysis_state': transitions[-1]['state'] if transitions else 'NOT_OBSERVED',
                'news_to_source_seconds': elapsed(row['observed_at'], news),
                'publication_to_news_receipt_seconds': elapsed(news, payload.get('news_published_at')),
                'analysis_queue_wait_seconds': elapsed(started, analysis[0] if analysis else None),
                'analysis_elapsed_seconds': elapsed(completed, started),
                'price_to_source_seconds': elapsed(row['observed_at'], first['observed_at'] if first else None),
                'price_to_analysis_seconds': elapsed(completed, first['observed_at'] if first else None),
                'price_to_news_receipt_seconds': elapsed(news, first['observed_at'] if first else None),
                'source_preceded_price': bool(first and row['observed_at'] < first['observed_at']),
                'price_event_causal_link': 'NOT_ASSERTED_SYMBOL_SESSION_ASSOCIATION_ONLY'})
    price = json.loads(first['payload']) if first else None
    search_started = next((s['observed_at'] for s in searches if s['state'] == 'RUNNING'), None)
    return {'ticker': symbol, 'session': session, 'as_of': cutoff,
            'price_first_seen_at': first['observed_at'] if first else None,
            'price_observation': price,
            'quote_to_detection_seconds': elapsed(first['observed_at'], price.get('price_at')) if first else None,
            'news_search_started_at': search_started,
            'price_to_news_search_seconds': elapsed(search_started, first['observed_at'] if first else None),
            'news_search_transitions': searches,
            'events': events, 'pipeline': queue.timeline(symbol, as_of),
            'scope': 'OBSERVED_CLOCKS_NOT_TRUE_MARKET_ONSET_OR_HISTORICAL_RECALL', 'external_requests': 0}
