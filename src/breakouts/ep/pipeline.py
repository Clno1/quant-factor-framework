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
    for pending in queue.ready('IDENTITY', clock(), limit=5000):
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
    ready = queue.ready('SOURCE', now, limit=5000)
    from .watch import source_priority
    priorities = {symbol: source_priority(queue, symbol, now) for symbol in {r['ticker'] for r in ready}}
    ready.sort(key=lambda row: (-priorities[row['ticker']], row['created_at'], row['job_id']))
    hints = {'EARNINGS', 'GUIDANCE', 'M_AND_A', 'COMMERCIAL_CONTRACT', 'DEAL_TERMINATION'}
    preferred = [r for r in ready if r['payload']['event'].get('event_type_hint') in hints]
    general = [r for r in ready if r['payload']['event'].get('event_type_hint') not in hints]
    # Hints allocate source-fetch capacity, never establish a catalyst as fact.
    # Reserve at least one in four slots for other news to avoid starvation.
    # Oldest due retries get reserved capacity; fresh noise cannot starve them.
    retries = [r for r in ready if r['attempts'] > 0]
    selected = retries[:max(1, limit // 4)]
    ids = {r['job_id'] for r in selected}
    selected += [r for r in preferred if r['job_id'] not in ids][:max(0, limit - max(1, limit // 4) - len(selected))]
    ids = {r['job_id'] for r in selected}
    selected += [r for r in general if r['job_id'] not in ids][:limit - len(selected)]
    ids = {r['job_id'] for r in selected}
    selected += [r for r in preferred if r['job_id'] not in ids][:limit - len(selected)]
    return selected


def process_sources(queue, store, resolver, config, *, clock, monotonic=time.monotonic):
    counts = Counter()
    profiles = store.profiles(clock())
    deadline = monotonic() + 120
    queue.consolidate_sources(clock())
    for pending in select_source_jobs(queue, clock(), config.source_jobs_per_cycle):
        if monotonic() >= deadline:
            break
        job = queue.claim('SOURCE', clock(), job_id=pending['job_id'])
        if job is None:
            continue
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
                queue.enqueue('ANALYSIS', symbol, original_id, result['text_revision'],
                              {'source_id': result['source_id']}, clock(), datetime.fromisoformat(job['expires_at']))
                for attachment in result.get('attachments', []):
                    if attachment['status'] == 'DOCUMENT_MATCHED':
                        document = store.source_detail(attachment['source_id'], as_of=clock())
                        queue.enqueue('ANALYSIS', symbol, digest(['official-original', symbol, canonical_url(attachment['url'])]),
                            document['parsed']['text_revision'], {'source_id': attachment['source_id']}, clock(),
                            datetime.fromisoformat(job['expires_at']))
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
    return dict(counts)
