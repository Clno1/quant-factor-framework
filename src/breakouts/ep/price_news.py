"""Bounded concurrent evidence collection with durable first-pass capacity."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import time

from .classifier import classify
from .models import EpSettings, digest, normalize_evidence, timestamp
from .pipeline import seed
from .service import CollectionBudget
from .catalyst import event_window


def select_news_jobs(queue, now, limit):
    ready = queue.ready('NEWS', now, limit=5000)
    from .price_discovery import price_priority
    priorities = {j['ticker']: price_priority(queue, j['ticker'], now) for j in ready}

    def rank(group):
        return group[:1] + sorted(group[1:], key=lambda j: (
            -priorities[j['ticker']], j['due_at'], j['job_id']))

    fresh = rank([j for j in ready if j['attempts'] == 0])
    retries = rank([j for j in ready if j['attempts'] > 0])
    retry_slots = max(1, limit // 4) if retries and limit > 1 else 0
    chosen = fresh[:limit - retry_slots] + retries[:retry_slots]
    chosen_ids = {j['job_id'] for j in chosen}
    chosen += [j for j in fresh + retries if j['job_id'] not in chosen_ids][:limit - len(chosen)]
    return ready, chosen


def _collect_job(queue, store, provider, budget, pending, clock):
    if not budget.available():
        return {'attempted': False, 'reason': 'BUDGET_DEFERRED'}
    job = queue.claim('NEWS', clock(), job_id=pending['job_id'])
    if not job:
        return {'attempted': False, 'reason': 'CLAIM_UNAVAILABLE'}
    started = clock()
    window = event_window(started)
    if job['payload']['session'] != window['session']:
        queue.finish(job, 'EXPIRED', 'OUTSIDE_CURRENT_EVENT_WINDOW', clock())
        return {'attempted': True, 'ticker': job['ticker'], 'reason': 'OUTSIDE_CURRENT_EVENT_WINDOW'}
    start, end = window['start'].date().isoformat(), job['payload']['session']
    run = None
    status, documents_seen, evidence = 'NO_NEWS_IN_FETCHED_SCOPE', 0, []
    successful_feeds = 0
    try:
        run = store.start_run(start, end, {'scope': 'PRICE_WATCH_SYMBOL_NEWS_FIRST_25_PER_FEED'}, clock())
        for feed in ('stock', 'press'):
            rows, request_status = budget.call(provider.symbol_news, job['ticker'], feed, start, end)
            now = clock()
            documents, rejected = [], 0
            if request_status == 'OK':
                if not isinstance(rows, list) or len(rows) > 25:
                    request_status = 'INVALID_NEWS_PAYLOAD'
                else:
                    for row in rows:
                        try:
                            doc = normalize_evidence('ARTICLE', row, now)
                            if doc.ticker != job['ticker'] or not window['start'] < datetime.fromisoformat(doc.published_at) <= now:
                                rejected += 1
                                continue
                            documents.append(doc)
                        except (ValueError, TypeError, KeyError, OverflowError):
                            rejected += 1
            store.save_page(run, {'feed': feed, 'ticker': job['ticker'], 'status': request_status,
                'received_at': timestamp(now), 'rejected': rejected, 'row_limit': 25,
                'possibly_truncated': isinstance(rows, list) and len(rows) == 25}, documents)
            events = []
            with store.connection() as db:
                for doc in documents:
                    row = db.execute('SELECT * FROM ep_documents WHERE document_id=? AND revision_id=?',
                                     (doc.document_id, doc.revision_id)).fetchone()
                    events.append(classify({**dict(row), 'payload': json.loads(row['payload_json'])}))
                    evidence.append((doc.document_id, doc.revision_id))
            seed(queue, {'run_id': run, 'candidates': [{'ticker': job['ticker'], 'events': events}]}, now)
            documents_seen += len(documents)
            if request_status != 'OK':
                status = request_status
                break
            successful_feeds += 1
        previous = job.get('result') or {}
        fingerprint = digest(sorted(set(evidence)))
        unchanged = previous.get('unchanged_passes', 0)
        if successful_feeds == 2:
            changed = bool(evidence) and fingerprint != previous.get('evidence_fingerprint')
            unchanged = 0 if changed else unchanged + 1
            status = 'NEWS_OBSERVED_CONTINUE_FOR_UPDATES' if evidence else 'NO_NEWS_IN_FETCHED_SCOPE'
        else:
            fingerprint = previous.get('evidence_fingerprint')
        # Quiet symbols back off; a changed evidence revision resets the delay.
        delay = min(1800, 300 * 2 ** min(3, max(0, unchanged - 1))) if successful_feeds == 2 else 300
        queue.finish(job, 'RETRY', status, clock(), retry_seconds=delay,
            result={'run_id': run, 'coverage': 'FIRST_25_PER_FEED_NOT_EXHAUSTIVE',
                    'evidence_fingerprint': fingerprint, 'unchanged_passes': unchanged,
                    'next_retry_seconds': delay, 'successful_feeds': successful_feeds})
    except Exception as exc:
        status = 'NEWS_PROCESSING_' + type(exc).__name__
        queue.finish(job, 'RETRY', status, clock(), retry_seconds=300)
    finally:
        if run:
            store.finish_run(run, clock(), {'status': status, 'scope': 'PRICE_WATCH_SYMBOL_NEWS_FIRST_25_PER_FEED'}, [])
    return {'attempted': True, 'ticker': job['ticker'], 'reason': status,
            'successful_feeds': successful_feeds,
            'news_documents_observed': documents_seen, 'started_at': timestamp(started),
            'finished_at': timestamp(clock()), 'first_pass': job['attempts'] == 1,
            'queue_wait_seconds': max(0, (started - datetime.fromisoformat(job['created_at'])).total_seconds()),
            'due_wait_seconds': max(0, (started - datetime.fromisoformat(job['due_at'])).total_seconds())}


def collect_price_news(queue, store, provider, config, *, clock, monotonic=time.monotonic):
    started = monotonic()
    cooldown = queue.checkpoint('news:provider_cooldown')
    if cooldown and clock() < datetime.fromisoformat(cooldown['until']):
        return {'status': 'PROVIDER_COOLDOWN', 'http_requests': 0, 'attempted_jobs': 0,
                'llm_requests': 0, 'discord_messages': 0, **cooldown}
    budget = CollectionBudget(EpSettings(max_requests=max(1, config.price_news_jobs * 2),
        deadline_seconds=getattr(config, 'price_news_deadline_seconds', 45)), monotonic)
    ready, selected = select_news_jobs(queue, clock(), config.price_news_jobs)
    results, counts = [], Counter()
    concurrency = getattr(config, 'price_news_concurrency', 1)
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='ep-news') as pool:
        futures = [pool.submit(_collect_job, queue, store, provider, budget, j, clock) for j in selected]
        for future in futures:
            try:
                result = future.result()
            except Exception as exc:
                result = {'attempted': False, 'reason': 'WORKER_FAILED_' + type(exc).__name__}
            results.append(result)
            counts[result['reason']] += 1
    attempted = sum(r['attempted'] for r in results)
    if budget.stop_reason:
        seconds = 300 if budget.stop_reason == 'PROVIDER_HTTP_429' else 3600
        queue.save_checkpoint('news:provider_cooldown', {'reason': budget.stop_reason,
            'until': timestamp(clock() + timedelta(seconds=seconds))}, clock())
    return {'counts': dict(counts), 'http_requests': budget.requests, 'llm_requests': 0, 'discord_messages': 0,
            'fully_checked_jobs': sum(r.get('successful_feeds') == 2 for r in results),
            'ready_in_bounded_scope': len(ready), 'scope_may_be_truncated': len(ready) == 5000,
            'attempted_jobs': attempted, 'first_pass_jobs': sum(r.get('first_pass', False) for r in results),
            'capacity_deferred': max(0, len(ready) - attempted), 'stop_reason': budget.stop_reason,
            'concurrency': concurrency, 'elapsed_seconds': round(monotonic() - started, 3),
            'selected_tickers': [j['ticker'] for j in selected], 'jobs': results}
