from datetime import timedelta
import json
import threading
import time
from types import SimpleNamespace

import pytest

from src.breakouts.ep.consumers import run_lane, lane_lock, consumer_status
from src.breakouts.ep.price_news import collect_price_news, select_news_jobs
from src.breakouts.ep.event_worker import WorkerConfig
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.store import EpStore
from src.breakouts.ep.models import timestamp
from test_ep_price_discovery import NOW, SESSION, identities, config


def pending(queue, number=40):
    for i in range(number):
        s = f'S{i:03}'
        queue.enqueue('NEWS', s, s, 'v1', {'session': SESSION}, NOW, NOW + timedelta(hours=7))


def worker(tmp_path, **kwargs):
    return WorkerConfig(database=str(tmp_path / 'evidence.db'), reviews_database=str(tmp_path / 'reviews.db'),
        output_directory=str(tmp_path / 'output'), key_file=str(tmp_path / 'unused.key'),
        queue_database=str(tmp_path / 'queue.db'), price_database=str(tmp_path / 'price.db'),
        enabled=True, collect_enabled=True, independent_consumers_enabled=True, **kwargs)


def test_concurrent_http_bounded_and_each_job_claimed_once(tmp_path):
    q, store = PipelineQueue(tmp_path / 'q.db'), EpStore(tmp_path / 'ep.db')
    pending(q)
    lock = threading.Lock()
    active, peak, calls = 0, 0, []
    class Provider:
        def symbol_news(self, s, feed, *args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append((s, feed))
            time.sleep(.01)
            with lock:
                active -= 1
            return []
    r = collect_price_news(q, store, Provider(), config(price_news_jobs=32, price_news_concurrency=2), clock=lambda: NOW)
    assert r['attempted_jobs'] == r['fully_checked_jobs'] == r['first_pass_jobs'] == 32
    assert peak == 2 and len(calls) == len(set(calls)) == 64
    assert r['capacity_deferred'] == 8
    with q.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE state='RUNNING'").fetchone()[0] == 0
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM ep_runs WHERE finished_at IS NULL').fetchone()[0] == 0


def test_first_pass_reservation_and_bounded_retry_share(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    pending(q, 80)
    for job in q.ready('NEWS', NOW)[:40]:
        claimed = q.claim('NEWS', NOW, job_id=job['job_id'])
        q.finish(claimed, 'RETRY', 'EMPTY', NOW, retry_seconds=1)
    _, selected = select_news_jobs(q, NOW + timedelta(seconds=2), 32)
    assert sum(j['attempts'] == 0 for j in selected) == 24
    assert sum(j['attempts'] > 0 for j in selected) == 8


def test_first_pass_capacity_survives_retries_across_minutes(tmp_path):
    q, store = PipelineQueue(tmp_path / 'q.db'), EpStore(tmp_path / 'e.db')
    pending(q, 412)
    for job in q.ready('NEWS', NOW)[:80]:
        q.finish(q.claim('NEWS', NOW, job_id=job['job_id']), 'RETRY', 'EMPTY', NOW, retry_seconds=1)
    now, first_passes = NOW + timedelta(seconds=2), 0
    for _ in range(14):
        result = collect_price_news(q, store, SimpleNamespace(symbol_news=lambda *a, **kw: []),
            config(price_news_jobs=32, price_news_concurrency=2), clock=lambda: now)
        first_passes += result['first_pass_jobs']
        now += timedelta(minutes=1)
    assert first_passes == 332
    with q.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE attempts=0').fetchone()[0] == 0


def test_preferred_source_retries_cannot_consume_fresh_slots(tmp_path):
    from src.breakouts.ep.pipeline import select_source_jobs
    from test_ep_event_queue_watch import payload, NOW as source_now
    q = PipelineQueue(tmp_path / 'q.db')
    for i in range(24):
        q.enqueue('SOURCE', f'S{i:03}', str(i), 'v1', payload(str(i)), source_now, source_now + timedelta(hours=2))
    for job in q.ready('SOURCE', source_now)[:16]:
        q.finish(q.claim('SOURCE', source_now, job_id=job['job_id']), 'RETRY', 'EMPTY', source_now, retry_seconds=1)
    selected = select_source_jobs(q, source_now + timedelta(seconds=2), 8)
    assert sum(r['attempts'] == 0 for r in selected) == 6
    assert sum(r['attempts'] > 0 for r in selected) == 2


def test_429_stops_new_requests_and_next_cycle_without_leasing_backlog(tmp_path):
    q, store = PipelineQueue(tmp_path / 'q.db'), EpStore(tmp_path / 'e.db')
    pending(q)
    class Refused:
        calls = 0
        def symbol_news(self, *args, **kwargs):
            self.calls += 1
            exc = RuntimeError('never expose credentials')
            exc.response = SimpleNamespace(status_code=429)
            raise exc
    provider = Refused()
    cfg = config(price_news_jobs=32, price_news_concurrency=2)
    r = collect_price_news(q, store, provider, cfg, clock=lambda: NOW)
    assert 1 <= r['http_requests'] <= 2
    assert r['stop_reason'] == 'PROVIDER_HTTP_429' and 'credentials' not in json.dumps(r)
    after = collect_price_news(q, store, provider, cfg, clock=lambda: NOW + timedelta(minutes=1))
    assert after['status'] == 'PROVIDER_COOLDOWN' and after['http_requests'] == 0
    assert len(q.ready('NEWS', NOW + timedelta(minutes=1))) >= 38


def test_empty_results_back_off_and_new_revision_resets_delay(tmp_path):
    q, store = PipelineQueue(tmp_path / 'q.db'), EpStore(tmp_path / 'e.db')
    pending(q, 1)
    rows = []
    provider = SimpleNamespace(symbol_news=lambda *a, **kw: rows)
    now = NOW
    for delay in [300, 600, 1200, 1800]:
        collect_price_news(q, store, provider, config(price_news_jobs=1), clock=lambda: now)
        job = q.explain('S000')['jobs'][0]
        assert job['result']['next_retry_seconds'] == delay
        now += timedelta(seconds=delay)
    rows.append({'symbol': 'S000', 'title': 'Company reports Q2 results',
        'publishedDate': timestamp(now - timedelta(seconds=1)), 'url': 'https://example.test/results', 'text': 'Results'})
    collect_price_news(q, store, provider, config(price_news_jobs=1), clock=lambda: now)
    news = next(j for j in q.explain('S000')['jobs'] if j['stage'] == 'NEWS')
    assert news['result']['next_retry_seconds'] == 300


def test_lane_locks_independent_and_same_lane_nonblocking(tmp_path):
    with lane_lock(tmp_path / 'q.db', 'news') as first:
        assert first
        with lane_lock(tmp_path / 'q.db', 'news') as second:
            assert not second
        with lane_lock(tmp_path / 'q.db', 'price') as price:
            assert price
    with lane_lock(tmp_path / 'q.db', 'news') as recovered:
        assert recovered


def test_news_lane_does_not_scan_prices_fetch_originals_or_run_model(tmp_path):
    cfg = worker(tmp_path)
    q = PipelineQueue(cfg.queue_database)
    pending(q, 2)
    provider = SimpleNamespace(symbol_news=lambda *a, **kw: [])
    result = run_lane(cfg, 'news', execute=True, clock=lambda: NOW,
                      provider=provider, identity_snapshot=identities(['S000', 'S001']))
    assert result['status'] == 'COMPLETED'
    assert result['work']['fully_checked_jobs'] == 2
    assert result['llm_requests'] == result['discord_messages'] == 0
    status = consumer_status(cfg, 'news', NOW)
    assert status['last_cycle']['status'] == 'COMPLETED'
    assert len(list((tmp_path / 'output/consumers/news').glob('*.json'))) == 2


def test_source_lane_with_empty_queue_needs_no_provider_and_price_lane_lock(tmp_path):
    cfg = worker(tmp_path, price_discovery_enabled=True)
    assert run_lane(cfg, 'source', execute=True, clock=lambda: NOW)['work']['status'] == 'IDLE'
    with lane_lock(cfg.queue_database, 'price'):
        assert run_lane(cfg, 'price', execute=True, clock=lambda: NOW)['status'] == 'ALREADY_RUNNING'


def test_schedule_warning_only_when_lane_expected(tmp_path):
    cfg = worker(tmp_path)
    PipelineQueue(cfg.queue_database)
    assert consumer_status(cfg, 'news', NOW)['warning']
    assert consumer_status(cfg, 'news', NOW + timedelta(days=1))['warning'] is None
    assert consumer_status(cfg, 'price', NOW)['warning'] is None
    assert consumer_status(cfg.model_copy(update={'collect_enabled': False}), 'news', NOW)['warning'] is None


def test_deadline_does_not_claim_whole_backlog(tmp_path):
    q, store = PipelineQueue(tmp_path / 'q.db'), EpStore(tmp_path / 'e.db')
    pending(q)
    elapsed = [0]
    def slow(*args, **kwargs):
        elapsed[0] += 10
        return []
    result = collect_price_news(q, store, SimpleNamespace(symbol_news=slow),
        config(price_news_jobs=32, price_news_concurrency=1, price_news_deadline_seconds=5),
        clock=lambda: NOW, monotonic=lambda: elapsed[0])
    assert result['http_requests'] == result['attempted_jobs'] == 1
    assert result['fully_checked_jobs'] == 0 and result['capacity_deferred'] == 39
    assert sum(j['attempts'] == 0 for j in q.ready('NEWS', NOW)) == 39


def test_interrupted_claim_recovers_without_accepting_old_worker_result(tmp_path):
    cfg = worker(tmp_path)
    q = PipelineQueue(cfg.queue_database)
    pending(q, 1)
    interrupted = q.claim('NEWS', NOW, lease_seconds=60)
    q.save_checkpoint('consumer:news', {'status': 'RUNNING', 'started_at': timestamp(NOW)}, NOW)
    assert not q.ready('NEWS', NOW + timedelta(seconds=59))
    later = NOW + timedelta(seconds=61)
    result = run_lane(cfg, 'news', execute=True, clock=lambda: later,
        provider=SimpleNamespace(symbol_news=lambda *a, **kw: []), identity_snapshot=identities(['S000']))
    assert result['work']['fully_checked_jobs'] == 1
    assert result['work']['first_pass_jobs'] == 0
    with pytest.raises(ValueError, match='LEASE_LOST'):
        q.finish(interrupted, 'COMPLETE', 'STALE_WORKER', later)
    assert consumer_status(cfg, 'news', later)['last_cycle']['status'] == 'COMPLETED'


def test_disabled_or_closed_market_creates_no_files(tmp_path):
    cfg = worker(tmp_path)
    assert run_lane(cfg, 'news')['status'] == 'PLAN_ONLY'
    assert run_lane(cfg, 'news', execute=True, clock=lambda: NOW + timedelta(days=1))['status'] == 'OUTSIDE_MARKET_WINDOW'
    with pytest.raises(ValueError, match='ENABLE_REQUIRED'):
        run_lane(cfg.model_copy(update={'independent_consumers_enabled': False}), 'news', execute=True, clock=lambda: NOW)
    assert not list(tmp_path.iterdir())


def test_split_feed_does_not_steal_consumer_jobs(tmp_path, monkeypatch):
    from src.breakouts.ep import event_ingest
    from test_ep_radar import FakeProvider, NOW as FEED_NOW
    from test_ep_pipeline import snapshot
    def forbidden(*args, **kwargs):
        raise AssertionError('consumer work must not execute in feed lane')
    monkeypatch.setattr(event_ingest, 'process_identities', forbidden)
    monkeypatch.setattr(event_ingest, 'consume_sources', forbidden)
    monkeypatch.setattr(event_ingest, 'load_issuer_index', lambda *a, **kw: (None, 'NOT_AVAILABLE', None))
    cfg = worker(tmp_path)
    result = event_ingest.ingest(cfg, clock=lambda: FEED_NOW, provider=FakeProvider(calendar=[]),
                                identity_snapshot=snapshot(['SNOW']))
    assert result['source_status'] == 'OWNED_BY_INDEPENDENT_CONSUMERS'
    assert PipelineQueue(cfg.queue_database).ready('IDENTITY', FEED_NOW)
