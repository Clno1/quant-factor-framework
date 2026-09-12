"""Independent one-shot EP lanes. No model or notification execution."""
from contextlib import contextmanager
from datetime import datetime, time as day_time, timezone
import fcntl
from pathlib import Path
import time

from .market_session import xnys_session_schedule
from .models import NEW_YORK, timestamp
from .pipeline import queue_path
from .queue import PipelineQueue
from src.utils.io import atomic_save_json

LANES = {'price', 'news', 'source'}


def market_window(now):
    local = now.astimezone(NEW_YORK)
    try:
        session = xnys_session_schedule(local.date().isoformat())
    except ValueError:
        return False
    return bool(session and datetime.combine(local.date(), day_time(4), NEW_YORK) <= now < session.closes_at)


@contextmanager
def lane_lock(path, lane):
    if lane not in LANES:
        raise ValueError('UNKNOWN_EP_CONSUMER')
    target = Path(path).with_suffix('.' + lane + '.lock')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('a+b') as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def backlog(queue, now):
    with queue.connection() as db:
        rows = db.execute('''SELECT stage,COUNT(*) AS outstanding,
            SUM(CASE WHEN attempts=0 THEN 1 ELSE 0 END) AS never_started,
            SUM(CASE WHEN state!='BLOCKED' AND due_at<=? AND (lease_until IS NULL OR lease_until<=?) THEN 1 ELSE 0 END) AS ready,
            MIN(created_at) AS oldest_created_at
            FROM jobs WHERE expires_at>? AND state IN ('PENDING','RETRY','RUNNING','BLOCKED')
            GROUP BY stage''', (timestamp(now), timestamp(now), timestamp(now))).fetchall()
    return [{**dict(r), 'oldest_age_seconds': max(0, (now - datetime.fromisoformat(r['oldest_created_at'])).total_seconds())}
            for r in rows]


def consumer_status(config, lane, now):
    if lane not in LANES:
        raise ValueError('UNKNOWN_EP_CONSUMER')
    path = Path(queue_path(config))
    if not path.exists():
        return {'status': 'QUEUE_NOT_INITIALIZED', 'external_requests': 0}
    queue = PipelineQueue(path, read_only=True)
    last = queue.checkpoint('consumer:' + lane)
    age = (now - datetime.fromisoformat(last.get('finished_at') or last['started_at'])).total_seconds() if last else None
    expected = (config.independent_consumers_enabled and config.enabled and config.collect_enabled
                and (lane != 'price' or config.price_discovery_enabled) and market_window(now))
    return {'lane': lane, 'enabled': config.independent_consumers_enabled, 'last_cycle': last,
            'seconds_since_last_observation': age, 'backlog': backlog(queue, now), 'external_requests': 0,
            'schedule_expected_now': expected,
            'warning': 'CHECK_SCHEDULE_OR_INTERRUPTED_RUN' if expected and (age is None or age > 180) else None}


def run_lane(config, lane, *, execute=False, clock=lambda: datetime.now(timezone.utc), provider=None,
             identity_snapshot=None, index_client=None, disclosure_client=None):
    if lane not in LANES:
        raise ValueError('UNKNOWN_EP_CONSUMER')
    enabled = config.independent_consumers_enabled and config.enabled and config.collect_enabled
    if not execute:
        return {'status': 'PLAN_ONLY', 'lane': lane, 'enabled': enabled,
                'external_requests': 0, 'llm_requests': 0, 'discord_messages': 0}
    if not enabled or (lane == 'price' and not config.price_discovery_enabled):
        raise ValueError('EXPLICIT_INDEPENDENT_CONSUMER_ENABLE_REQUIRED')
    now = clock()
    if not market_window(now):
        return {'status': 'OUTSIDE_MARKET_WINDOW', 'lane': lane, 'external_requests': 0}
    with lane_lock(queue_path(config), lane) as acquired:
        if not acquired:
            return {'status': 'ALREADY_RUNNING', 'lane': lane, 'external_requests': 0}
        queue = PipelineQueue(queue_path(config))
        queue.expire(now)
        result = {'status': 'RUNNING', 'lane': lane, 'started_at': timestamp(now),
                  'llm_requests': 0, 'discord_messages': 0, 'before': backlog(queue, now)}
        queue.save_checkpoint('consumer:' + lane, result, now)
        tick = time.monotonic()
        try:
            from .provider import FmpEpProvider
            from .identity import load_identity_snapshot
            from .store import EpStore
            provider = provider or FmpEpProvider()
            if lane in {'price', 'news'}:
                snapshot = identity_snapshot or load_identity_snapshot(now,
                    catalog_path=config.identity_catalog_path or None,
                    snapshot_root=config.identity_snapshot_root or None, source_root=config.identity_source_root or None)
            if lane == 'price':
                from .price_discovery import PriceStore, discover
                result['work'] = discover(queue, PriceStore(config.price_database), snapshot, provider, config, clock=clock)
            elif lane == 'news':
                from .price_news import collect_price_news
                from .pipeline import process_identities
                store = EpStore(config.database)
                result['work'] = collect_price_news(queue, store, provider, config, clock=clock)
                fallback = min(2, config.identity_fallback_requests)
                if result['work'].get('stop_reason') or result['work'].get('status') == 'PROVIDER_COOLDOWN':
                    fallback = 0
                result['identities'] = process_identities(queue, store, provider, snapshot,
                    config.model_copy(update={'identity_fallback_requests': fallback}), clock=clock)
            else:
                from .event_ingest import load_issuer_index, registry_for_candidates, consume_sources
                if not queue.ready('SOURCE', now, limit=1):
                    result['work'] = {'status': 'IDLE', 'http_requests': 0}
                else:
                    cached, status, client = load_issuer_index(config, clock=clock, index_client=index_client)
                    symbols = {r['ticker'] for r in queue.ready('SOURCE', clock(), limit=5000)}
                    registry = registry_for_candidates(cached['payload'], symbols)[0] if cached else {'issuers': {}}
                    counts, source_status, requests, public_requests = consume_sources(config, queue, EpStore(config.database), registry,
                        clock=clock, disclosure_client=disclosure_client)
                    result['work'] = {'counts': counts, 'status': source_status, 'index_status': status,
                                      'http_requests': requests + public_requests + getattr(client, 'requests', 0),
                                      'execution': queue.checkpoint('source:cycle') if source_status == 'QUEUED_SOURCE_PASS_COMPLETED' else None}
            result['status'] = 'COMPLETED'
            work = result['work']
            if (work.get('stop_reason') or work.get('status') in {'BASELINE_UNAVAILABLE',
                    'PRICE_FETCH_FAILED_CURSOR_RETAINED', 'CURRENT_SECURITY_IDENTITIES_UNAVAILABLE',
                    'SOURCE_ACCESS_NOT_CONFIGURED_JOBS_RETAINED'}
                    or any(k.startswith(('WORKER_FAILED_', 'NEWS_PROCESSING_')) for k in work.get('counts', {}))):
                result['status'] = 'DEGRADED'
        except Exception as exc:
            result.update(status='FAILED', error_type=type(exc).__name__)
        result.update(finished_at=timestamp(clock()), elapsed_seconds=round(time.monotonic() - tick, 3),
                      after=backlog(queue, clock()))
        queue.save_checkpoint('consumer:' + lane, result, clock())
        root = Path(config.output_directory) / 'consumers' / lane
        atomic_save_json(result, root / (result['started_at'].replace(':', '-') + '.json'))
        atomic_save_json(result, root / 'latest.json')
        return result
