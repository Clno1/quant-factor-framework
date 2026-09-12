"""Bounded queue execution: discovery -> identity -> original -> analysis."""
from collections import Counter
from datetime import datetime, timedelta
import time

from src.data.public_articles import SourceAccessError

from .catalyst import event_window
from .identity import current_profile
from .models import EpSettings, canonical_url, digest, timestamp
from .service import CollectionBudget


def queue_path(config):
    return config.queue_database or str(config.database) + '.pipeline.sqlite3'


def eligibility(profile):
    if profile.get('asset_type') not in {'STOCK', 'ADR'}:
        return 'SECURITY_TYPE_EXCLUDED'
    if profile.get('exchange') not in {'NASDAQ', 'NYSE', 'AMEX'}:
        return 'EXCHANGE_EXCLUDED'
    if profile.get('is_actively_trading') is not True:
        return 'INACTIVE_OR_UNVERIFIED_SECURITY'
    return None


def seed(queue, report, now):
    for candidate in report['candidates']:
        for event in candidate['events']:
            if 'url' not in event['evidence'] or not event.get('published_at'):
                continue
            expires = datetime.fromisoformat(event['published_at']) + timedelta(hours=72)
            queue.enqueue('IDENTITY', candidate['ticker'], event['document_id'], event['revision_id'],
                          {'run_id': report['run_id'], 'event': event}, now, expires)


def process_identities(queue, store, provider, snapshot, config, *, clock, monotonic=time.monotonic):
    budget = CollectionBudget(EpSettings(max_requests=max(1, config.identity_fallback_requests),
                              deadline_seconds=60), monotonic)
    profiles = store.profiles(clock())
    counts = Counter()
    for pending in queue.ready('IDENTITY', clock(), limit=getattr(config, 'identity_jobs_per_cycle', 5000)):
        if monotonic() >= budget.ends_at:
            break
        job = queue.claim('IDENTITY', clock(), job_id=pending['job_id'])
        if job is None:
            continue
        symbol, payload = job['ticker'], job['payload']
        event = payload['event']
        try:
            if datetime.fromisoformat(event['published_at']) <= event_window(clock())['start']:
                queue.finish(job, 'EXPIRED', 'OUTSIDE_CURRENT_EVENT_WINDOW', clock())
                counts['OUTSIDE_CURRENT_EVENT_WINDOW'] += 1
                continue
            if event['event_type_hint'] == 'LEGAL_NOTICE':
                queue.finish(job, 'EXCLUDED', 'LEGAL_NOTICE', clock())
                counts['LEGAL_NOTICE'] += 1
                continue
            record = profiles.get(symbol)
            if symbol in snapshot.rejected:
                queue.finish(job, 'RETRY', snapshot.rejected[symbol], clock(), retry_seconds=900)
                counts[snapshot.rejected[symbol]] += 1
                continue
            if not current_profile(record, clock()):
                bulk = snapshot.profile(symbol, clock())
                if bulk:
                    store.save_profile(payload['run_id'], symbol, clock(), 'OK', bulk)
                    record = {'profile': bulk, 'status': 'OK', 'observed_at': timestamp(clock())}
                    profiles[symbol] = record
                elif (record and record.get('status') != 'OK'
                      and timedelta(0) <= clock() - datetime.fromisoformat(record['observed_at']) < timedelta(minutes=15)):
                    queue.finish(job, 'RETRY', 'RECENT_IDENTITY_FAILURE_CACHED', clock(),
                                 retry_seconds=900, result={'provider_status': record['status']})
                    counts['RECENT_IDENTITY_FAILURE_CACHED'] += 1
                    continue
                elif budget.requests >= config.identity_fallback_requests:
                    queue.finish(job, 'RETRY', 'IDENTITY_REQUEST_CAPACITY_DEFERRED', clock())
                    counts['IDENTITY_REQUEST_CAPACITY_DEFERRED'] += 1
                    continue
                else:
                    profile, status = budget.call(provider.profile, symbol)
                    if status == 'OK' and (not isinstance(profile, dict) or profile.get('ticker') != symbol):
                        profile, status = None, 'PROFILE_NOT_FOUND_OR_MISMATCH'
                    store.save_profile(payload['run_id'], symbol, clock(), status, profile)
                    record = {'profile': profile, 'status': status, 'observed_at': timestamp(clock())}
                    profiles[symbol] = record
            if not current_profile(record, clock()):
                reason = record['status'] if record else 'IDENTITY_NOT_AVAILABLE'
                queue.finish(job, 'RETRY', reason, clock(), retry_seconds=900)
                counts[reason] += 1
                continue
            reason = eligibility(record['profile'])
            if reason:
                queue.finish(job, 'EXCLUDED', reason, clock())
                counts[reason] += 1
                continue
            queue.enqueue_event(symbol, payload, clock(), datetime.fromisoformat(job['expires_at']))
            queue.finish(job, 'COMPLETE', 'ELIGIBLE_IDENTITY_RESOLVED', clock(),
                         result={'identity_source': record['profile'].get('identity_source', 'FMP_SINGLE_PROFILE'),
                                 'identity_version': record['profile'].get('identity_version')})
            counts['ELIGIBLE_IDENTITY_RESOLVED'] += 1
        except (ValueError, KeyError, TypeError, OSError, SourceAccessError) as exc:
            queue.finish(job, 'RETRY', 'IDENTITY_PROCESSING_' + type(exc).__name__, clock())
            counts['IDENTITY_PROCESSING_ERROR'] += 1
    return {'counts': dict(counts), 'http_requests': budget.requests}


def select_source_jobs(queue, now, limit):
    from .classifier import routing_hint
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('INVALID_SOURCE_SELECTION_LIMIT')
    ready = queue.ready('SOURCE', now, limit=5000)
    start = event_window(now)['start']
    ready = [r for r in ready if start < datetime.fromisoformat(r['payload']['event']['published_at']) <= now
             and datetime.fromisoformat(r['created_at']) <= now]
    from .watch import source_priority
    priorities = {symbol: source_priority(queue, symbol, now) for symbol in {r['ticker'] for r in ready}}
    ready.sort(key=lambda row: (-priorities[row['ticker']], row['created_at'], row['job_id']))
    hints = {'EARNINGS', 'GUIDANCE', 'M_AND_A', 'COMMERCIAL_CONTRACT', 'DEAL_TERMINATION'}
    fresh = [r for r in ready if r['attempts'] == 0]
    preferred = [r for r in fresh if routing_hint(r['payload']['event']) in hints]
    general = [r for r in fresh if routing_hint(r['payload']['event']) not in hints]
    # Hints allocate source-fetch capacity, never establish a catalyst as fact.
    # Reserve at least one in four slots for other news to avoid starvation.
    # Retry rows must not re-enter via the preferred pool and consume fresh slots.
    retries = [r for r in ready if r['attempts'] > 0]
    retries.sort(key=lambda r: (r['due_at'], r['created_at'], r['job_id']))
    retry_slots = max(1, limit // 4) if retries and limit > 1 else 0
    fresh_slots = limit - retry_slots
    selected, ids, symbols = [], set(), Counter()
    def take(pool, target, per_symbol=1):
        for row in pool:
            if len(selected) >= target:
                break
            if row['job_id'] not in ids and symbols[row['ticker']] < per_symbol:
                selected.append(row)
                ids.add(row['job_id'])
                symbols[row['ticker']] += 1
    take([r for r in fresh if priorities[r['ticker']] > 0], min(1, fresh_slots))
    take(preferred, max(len(selected), fresh_slots - (max(1, limit // 4) if general and fresh_slots > 1 else 0)))
    take(general, fresh_slots)
    take(fresh, fresh_slots)
    take(retries, limit)
    take(ready, limit)
    # A second event (for example earnings plus M&A) can use remaining capacity,
    # but dozens of headlines for one issuer cannot monopolize a cycle.
    take(ready, limit, per_symbol=2)
    return selected


def source_plan(queue, now, limit, *, selected=None):
    """Read-only view of the current queue, not a reconstruction of past state."""
    from .classifier import routing_hint
    ready = queue.ready('SOURCE', now, limit=5000)
    if selected is None:
        selected = select_source_jobs(queue, now, limit)
    return {'version': 'ep-source-routing-v3', 'evaluated_at': timestamp(now),
            'scope': 'CURRENT_QUEUE_SNAPSHOT_NOT_HISTORICAL_REPLAY',
            'ready_in_bounded_scope': len(ready), 'scan_limit': 5000,
            'scan_may_be_truncated': len(ready) == 5000,
            'selected_first_pass': sum(r['attempts'] == 0 for r in selected),
            'selected_retries': sum(r['attempts'] > 0 for r in selected),
            'routing_hints': dict(Counter(routing_hint(r['payload']['event']) for r in ready)),
            'selected': [{'job_id': r['job_id'], 'ticker': r['ticker'], 'attempts': r['attempts'],
                          'routing_hint': routing_hint(r['payload']['event']),
                          'original_hint': r['payload']['event'].get('event_type_hint'),
                          'title': r['payload']['event']['evidence']['title'],
                          'queued_at': r['created_at'],
                          'first_seen_at': r['payload']['event'].get('first_seen_at'),
                          'wait_seconds': max(0, (now - datetime.fromisoformat(r['created_at'])).total_seconds())}
                         for r in selected], 'external_requests': 0, 'discord_messages': 0}


def process_sources(queue, store, resolver, config, *, clock, monotonic=time.monotonic):
    counts = Counter()
    started, executions = clock(), []
    profiles = store.profiles(clock())
    deadline = monotonic() + getattr(config, 'source_deadline_seconds', 120)
    queue.consolidate_sources(clock())
    now = clock()
    selected = select_source_jobs(queue, now, config.source_jobs_per_cycle)
    queue.save_checkpoint('source:selection', source_plan(queue, now, config.source_jobs_per_cycle, selected=selected), now)
    for pending in selected:
        if monotonic() >= deadline:
            break
        job = queue.claim('SOURCE', clock(), job_id=pending['job_id'])
        if job is None:
            continue
        executions.append({'job_id': job['job_id'], 'ticker': job['ticker'],
            'first_pass': job['attempts'] == 1, 'started_at': timestamp(clock()),
            'queue_wait_seconds': max(0, (clock() - datetime.fromisoformat(job['created_at'])).total_seconds())})
        payload, symbol = job['payload'], job['ticker']
        if payload.get('event_group'):
            members = queue.event_members(payload['event_group']['event_id'], clock())
            if members:
                # A failed syndicated headline must not permanently pin the event.
                payload = members[(job['attempts'] - 1) % len(members)]
        try:
            if datetime.fromisoformat(payload['event']['published_at']) <= event_window(clock())['start']:
                queue.finish(job, 'EXPIRED', 'OUTSIDE_CURRENT_EVENT_WINDOW', clock())
                counts['OUTSIDE_CURRENT_EVENT_WINDOW'] += 1
                continue
            record = profiles.get(symbol)
            if not current_profile(record, clock()):
                queue.finish(job, 'RETRY', 'CURRENT_IDENTITY_REQUIRED', clock())
                counts['CURRENT_IDENTITY_REQUIRED'] += 1
                continue
            reason = eligibility(record['profile'])
            if reason:
                queue.finish(job, 'EXCLUDED', reason, clock())
                counts[reason] += 1
                continue
            profile = record['profile']
            registry = resolver.registry.get('issuers', {}).get(symbol, {})
            if profile.get('cik') and registry.get('cik') and str(profile['cik']).zfill(10) != registry['cik']:
                queue.finish(job, 'BLOCKED', 'ISSUER_CIK_CONFLICT', clock())
                counts['ISSUER_CIK_CONFLICT'] += 1
                continue
            result = resolver.resolve_event(payload['run_id'], {'ticker': symbol, 'identity': profile}, payload['event'])
            status = result['status']
            counts[status] += 1
            if status == 'DOCUMENT_MATCHED':
                # Different news syndications of one official text share one analysis job.
                original_id = digest(['official-original', symbol, canonical_url(result['final_url'])])
                analysis_id = queue.enqueue('ANALYSIS', symbol, original_id, result['text_revision'],
                              {'source_id': result['source_id']}, clock(), datetime.fromisoformat(job['expires_at']))
                from .latency import record as record_latency
                record_latency(queue, symbol, event_window(clock())['session'], 'SOURCE_MATCHED', digest([job['document_id'], analysis_id]), clock(),
                       {'source_id': result['source_id'], 'source_job_id': job['job_id'],
                        'event_id': job['document_id'], 'analysis_job_id': analysis_id,
                        'news_document_id': payload['event']['document_id'],
                        'news_first_seen_at': payload['event'].get('first_seen_at'),
                        'news_published_at': payload['event'].get('published_at')})
                for attachment in result.get('attachments', []):
                    if attachment['status'] == 'DOCUMENT_MATCHED':
                        document = store.source_detail(attachment['source_id'], as_of=clock())
                        attachment_job = queue.enqueue('ANALYSIS', symbol, digest(['official-original', symbol, canonical_url(attachment['url'])]),
                            document['parsed']['text_revision'], {'source_id': attachment['source_id']}, clock(),
                            datetime.fromisoformat(job['expires_at']))
                        record_latency(queue, symbol, event_window(clock())['session'], 'SOURCE_MATCHED',
                            digest([job['document_id'], attachment_job]), clock(),
                            {'source_id': attachment['source_id'], 'source_job_id': job['job_id'],
                             'event_id': job['document_id'], 'analysis_job_id': attachment_job, 'is_attachment': True,
                             'news_document_id': payload['event']['document_id'],
                             'news_first_seen_at': payload['event'].get('first_seen_at'),
                             'news_published_at': payload['event'].get('published_at')})
                queue.finish(job, 'COMPLETE', 'ORIGINAL_TEXT_MATCHED', clock(), result={'source_id': result['source_id']})
            else:
                queue.finish(job, 'RETRY', status, clock(), result={
                    'source_id': result.get('source_id'), 'source_route': result.get('source_route'),
                    'blocking_reasons': result.get('blocking_reasons', result.get('incomplete_reasons', [])),
                    'ir_blocking_reasons': result.get('ir_attempt', {}).get('blocking_reasons', [])},
                             retry_seconds=min(3600, 300 * 2 ** min(job['attempts'] - 1, 4)))
        except (ValueError, KeyError, TypeError, OSError, SourceAccessError) as exc:
            queue.finish(job, 'RETRY', 'SOURCE_PROCESSING_' + type(exc).__name__, clock())
            counts['SOURCE_PROCESSING_ERROR'] += 1
    queue.save_checkpoint('source:cycle', {'started_at': timestamp(started), 'finished_at': timestamp(clock()),
        'attempted_jobs': len(executions), 'selected_jobs': len(selected),
        'first_pass_jobs': sum(r['first_pass'] for r in executions), 'jobs': executions, 'counts': dict(counts)}, clock())
    return dict(counts)
