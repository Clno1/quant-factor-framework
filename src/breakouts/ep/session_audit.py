"""Read-only, point-in-time EP acceptance. Missing observations are not market misses."""
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import sqlite3

from .latency import elapsed
from .market_session import xnys_session_schedule
from .models import NEW_YORK, ticker, timestamp


def read_prices(path, start, cutoff):
    if not path or not Path(path).is_file():
        return {}, [], 'PRICE_DATABASE_NOT_INITIALIZED'
    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        # Filter before ranking: today's/latest mutable cursor is not historical evidence.
        rows = db.execute('''WITH ranked AS (
            SELECT ticker,observed_at,payload,
                ROW_NUMBER() OVER(PARTITION BY ticker ORDER BY observed_at DESC,rowid DESC) AS rank,
                MIN(observed_at) OVER(PARTITION BY ticker) AS first_scanned_at,
                MIN(CASE WHEN json_extract(payload,'$.status')='PRICE_WATCH' THEN observed_at END)
                    OVER(PARTITION BY ticker) AS first_watch_at
            FROM ep_price_rows WHERE observed_at>=? AND observed_at<=?)
            SELECT * FROM ranked WHERE rank=1''', (start, cutoff)).fetchall()
        prices = {}
        for row in rows:
            value = json.loads(row['payload'])
            prices[row['ticker']] = {k: value.get(k) for k in
                ('observed_at', 'price_at', 'status', 'reason', 'indicative_gap_pct', 'market_phase')}
            prices[row['ticker']].update(first_scanned_at=row['first_scanned_at'], first_watch_at=row['first_watch_at'])
        scans = [json.loads(r[0]) for r in db.execute('''SELECT payload FROM ep_price_scans
            WHERE started_at>=? AND json_extract(payload,'$.finished_at')<=?
            ORDER BY started_at''', (start, cutoff))]
    return prices, scans, 'OBSERVED_ROWS_ONLY'


def history_jobs(db, cutoff, start):
    rows = db.execute('''SELECT j.job_id,j.stage,j.ticker,j.created_at,j.expires_at,
        t.observed_at,t.state,t.reason,t.id FROM jobs j JOIN transitions t ON j.job_id=t.job_id
        WHERE j.created_at<=? AND j.expires_at>? AND t.observed_at<=?
        ORDER BY t.observed_at,t.id''', (cutoff, start, cutoff)).fetchall()
    jobs = {}
    for row in rows:
        item = jobs.setdefault(row['job_id'], {'job_id': row['job_id'], 'stage': row['stage'],
            'ticker': row['ticker'], 'queued_at': row['created_at'], 'expires_at': row['expires_at'],
            'first_started_at': None, 'first_completed_at': None})
        if row['state'] == 'RUNNING' and item['first_started_at'] is None:
            item['first_started_at'] = row['observed_at']
        if row['state'] == 'COMPLETE' and item['first_completed_at'] is None:
            item['first_completed_at'] = row['observed_at']
        item.update(state=row['state'], reason=row['reason'], state_observed_at=row['observed_at'])
    for job in jobs.values():
        job['age_seconds'] = elapsed(cutoff, job['queued_at'])
        job['queue_wait_seconds'] = elapsed(job['first_started_at'], job['queued_at'])
        job['retention_elapsed'] = job['expires_at'] <= cutoff
    return jobs


def event_clocks(record, jobs):
    payload = json.loads(record['payload'])
    analysis = jobs.get(payload.get('analysis_job_id'), {})
    return {**payload, 'source_matched_at': record['observed_at'],
        'analysis_queued_at': analysis.get('queued_at'),
        'analysis_started_at': analysis.get('first_started_at'),
        'analysis_completed_at': analysis.get('first_completed_at'),
        'analysis_state': analysis.get('state', 'NOT_OBSERVED'),
        'analysis_reason': analysis.get('reason'),
        'news_to_source_seconds': elapsed(record['observed_at'], payload.get('news_first_seen_at')),
        'source_to_analysis_start_seconds': elapsed(analysis.get('first_started_at'), record['observed_at']),
        'analysis_queue_wait_seconds': analysis.get('queue_wait_seconds'),
        'analysis_elapsed_seconds': elapsed(analysis.get('first_completed_at'), analysis.get('first_started_at'))}


def blockers(price, jobs, events):
    result = []
    if not price:
        result.append('PRICE_NOT_OBSERVED_IN_CAPTURED_SCOPE')
    elif not price.get('first_watch_at'):
        result.append('PRICE_BELOW_THRESHOLD' if price['status'] == 'BELOW_THRESHOLD'
                      else 'PRICE_INPUT_BLOCKED:' + str(price['reason']))
    if price and price.get('first_watch_at') and not any(j['stage'] == 'NEWS' for j in jobs):
        result.append('PRICE_WATCH_WITHOUT_OBSERVED_NEWS_JOB')
    linked = {e.get(key) for e in events for key in ('analysis_job_id', 'source_job_id')}
    for job in jobs:
        # A lawyer notice or failed alternative story cannot veto a matched event.
        # Other jobs remain individually inspectable, with their own IDs, below.
        if events and job['stage'] != 'NEWS' and job['job_id'] not in linked:
            continue
        if job['state'] in {'PENDING', 'RUNNING', 'RETRY', 'BLOCKED', 'EXCLUDED', 'EXPIRED'}:
            result.append(':'.join((job['stage'], job['state'], job['reason'])))
    if not events:
        result.append('NO_SOURCE_MATCH_CLOCK_IN_THIS_SESSION')
    elif any(e['analysis_completed_at'] for e in events):
        result.append('ANALYSIS_COMPLETED_NOT_FACT_APPROVAL_OR_TRADE_CONFIRMATION')
    return sorted(set(result))


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    low = int(index)
    return round(ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low), 3)


def report(queue, price_path, session, as_of, *, symbols=(), captured_at=None):
    captured_at = captured_at or datetime.now(timezone.utc)
    cutoff, captured = timestamp(as_of), timestamp(captured_at)
    if cutoff > captured:
        raise ValueError('ASOF_CANNOT_EXCEED_CAPTURE_TIME')
    if date.fromisoformat(session).isoformat() != session:
        raise ValueError('CANONICAL_SESSION_DATE_REQUIRED')
    schedule = xnys_session_schedule(session)
    start = timestamp(datetime.combine(date.fromisoformat(session), time(4), NEW_YORK))
    base = {'session': session, 'as_of': cutoff, 'captured_at': captured, 'window_start': start,
        'session_close': timestamp(schedule.closes_at), 'external_requests': 0,
        'llm_requests': 0, 'discord_messages': 0, 'strategy_accepted': False,
        'market_recall': None, 'scope': 'OBSERVED_HISTORY_NOT_TRUE_MARKET_ONSET_OR_WHOLE_MARKET_RECALL'}
    if cutoff < start:
        return {**base, 'status': 'WAITING_FOR_SESSION', 'symbols': [], 'queue_summary': []}
    # A session report never imports a later day's prices or subsequent evidence.
    cutoff = min(cutoff, timestamp(schedule.closes_at))
    base['observation_cutoff'] = cutoff
    prices, scans, price_scope = read_prices(price_path, start, cutoff)
    with queue.connection() as db:
        db.execute('BEGIN')
        jobs = history_jobs(db, cutoff, start)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        clocks = [dict(r) for r in db.execute('''SELECT * FROM latency_events
            WHERE session=? AND observed_at<=? ORDER BY observed_at,id''', (session, cutoff))] if 'latency_events' in names else []
        members = [dict(r) for r in db.execute('''SELECT e.ticker,m.event_id,m.observed_at,m.payload
            FROM event_members m JOIN events e ON e.event_id=m.event_id
            WHERE m.observed_at<=? AND EXISTS(SELECT 1 FROM jobs j WHERE j.ticker=e.ticker
                AND j.document_id=e.event_id AND j.created_at<=? AND j.expires_at>?)
            ORDER BY m.observed_at''', (cutoff, cutoff, start))] if 'event_members' in names else []
    by_symbol, by_event, evidence = defaultdict(list), defaultdict(list), defaultdict(list)
    discovered = {}
    for member in members:
        event = json.loads(member['payload']).get('event', {})
        evidence[member['ticker']].append({'event_id': member['event_id'],
            'news_document_id': event.get('document_id'), 'revision_id': event.get('revision_id'),
            'news_first_seen_at': event.get('first_seen_at'), 'news_published_at': event.get('published_at'),
            'event_member_attached_at': member['observed_at']})
    cohort = {ticker(s) for s in symbols}
    cohort.update(s for s, p in prices.items() if p['first_watch_at'])
    for record in clocks:
        cohort.add(record['ticker'])
        if record['kind'] == 'PRICE_DISCOVERED':
            discovered.setdefault(record['ticker'], record['observed_at'])
        if record['kind'] == 'SOURCE_MATCHED':
            by_event[record['ticker']].append(event_clocks(record, jobs))
    for job in jobs.values():
        by_symbol[job['ticker']].append(job)
    items = []
    for symbol in sorted(cohort):
        events, pending, price = by_event[symbol], by_symbol[symbol], prices.get(symbol)
        first_watch = (price.get('first_watch_at') if price else None) or discovered.get(symbol)
        for event in events:
            event['price_to_analysis_seconds'] = elapsed(event['analysis_completed_at'], first_watch)
            event['price_event_causal_link'] = 'SYMBOL_SESSION_ASSOCIATION_NOT_ASSERTED_CAUSAL'
        # These queue jobs can include other stories: only source->analysis uses exact IDs.
        items.append({'ticker': symbol, 'price': price, 'events': events,
            'evidence_members': evidence[symbol], 'queue_jobs': pending,
            'price_discovered_at': first_watch,
            'news_search_started_at': min((j['first_started_at'] for j in pending
                if j['stage'] == 'NEWS' and j['first_started_at']), default=None),
            'queue_scope': 'ACTIVE_RETENTION_JOBS_MAY_INCLUDE_OTHER_EVENTS',
            'reasons': blockers(price, pending, events)})
    grouped = Counter((j['stage'], j['state'], j['reason']) for j in jobs.values())
    metrics = {}
    for name in ('news_to_source_seconds', 'analysis_queue_wait_seconds', 'analysis_elapsed_seconds', 'price_to_analysis_seconds'):
        # One original can support several event members. Do not overweight shared analyses.
        unique = {e.get('analysis_job_id'): e.get(name) for item in items for e in item['events'] if e.get(name) is not None}
        values = list(unique.values())
        metrics[name] = {'observed_count': len(values), 'p50': percentile(values, .5), 'p95': percentile(values, .95)}
    return {**base, 'status': 'OBSERVED_ONLY_NOT_ACCEPTED' if prices or clocks else 'NO_SESSION_OBSERVATIONS',
        'price_scope': price_scope, 'price_scanned_symbols': len(prices),
        'price_watch_symbols': sum(bool(p['first_watch_at']) for p in prices.values()),
        'completed_scan_records': len(scans), 'scan_status_counts': dict(Counter(s['status'] for s in scans)),
        'cohort_scope': 'PRICE_WATCH_OR_SOURCE_MATCHED_THIS_SESSION_PLUS_REQUESTED_SYMBOLS',
        'price_last_scan_finished_at': scans[-1].get('finished_at') if scans else None,
        'queue_summary': [{'stage': a, 'state': b, 'reason': c, 'count': n} for (a, b, c), n in sorted(grouped.items())],
        'metrics': metrics, 'symbols': items,
        'limitations': ['NO_DELIVERY_RECEIPT_ASSERTION', 'NO_VOLUME_OR_RATING_ACCEPTANCE',
            'QUEUE_WAIT_IS_NOT_PROOF_OF_CAPACITY_CAUSE', 'MISSING_LEGACY_CLOCKS_ARE_NOT_BACKFILLED',
            'STAGE_LATENCIES_ONLY_COVER_COMPLETED_OBSERVATIONS_NOT_PENDING_FAILURES',
            'SESSION_OBSERVATION_CUTOFF_CAPPED_AT_EXCHANGE_CLOSE']}


def consumer_coverage(directory, session, as_of):
    """Read immutable per-run files, never current checkpoints for a historical report."""
    schedule = xnys_session_schedule(session)
    start = datetime.combine(date.fromisoformat(session), time(4), NEW_YORK)
    end = min(as_of, schedule.closes_at)
    result = {}
    for lane, offset in [('price', 0), ('news', 15), ('source', 35)]:
        records, invalid = [], 0
        for path in sorted((Path(directory) / 'consumers' / lane).glob(session + 'T*.json')):
            try:
                value = json.loads(path.read_text())
                began = datetime.fromisoformat(value['started_at'])
                finished = datetime.fromisoformat(value['finished_at'])
                if start <= began < schedule.closes_at and began <= finished <= as_of:
                    records.append(value)
            except (ValueError, KeyError, TypeError, OSError):
                invalid += 1
        duration = (end - start).total_seconds()
        # Close is exclusive, while an as-of inside the session includes its scheduled instant.
        expected = max(0, int((duration - offset) // 60) + 1) if duration >= offset else 0
        if end == schedule.closes_at and offset == 0:
            expected = max(0, expected - 1)
        minutes = {int((datetime.fromisoformat(r['started_at']) - start).total_seconds() // 60) for r in records}
        elapsed_values = [r['elapsed_seconds'] for r in records if isinstance(r.get('elapsed_seconds'), (int, float))]
        result[lane] = {'expected_schedule_slots': expected, 'completed_records': len(records),
            'invalid_record_files': invalid,
            'observed_start_minutes': len(minutes), 'unobserved_schedule_slots': max(0, expected - len(minutes)),
            'status_counts': dict(Counter(r['status'] for r in records)),
            'elapsed_p95_seconds': percentile(elapsed_values, .95),
            'last_finished_at': records[-1]['finished_at'] if records else None,
            'last_backlog': records[-1].get('after', []) if records else [],
            'scope': 'RECORDED_RUNS_NOT_SYSTEMD_START_PROOF_OR_SLA_PASS'}
    return result
