from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import json

import pandas as pd
import pytest

from src.breakouts.ep.identity import IdentitySnapshot, _normalize, from_provider_manifest, current_profile
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.pipeline import seed, process_identities, process_sources
from src.breakouts.ep.service import EpRadar
from src.breakouts.ep.models import EpSettings
from src.breakouts.ep.store import EpStore
from src.breakouts.ep.event_worker import WorkerConfig, cycle
from test_ep_radar import NOW, FakeProvider, article
from test_ep_llm import seeded, CLOCK
from test_ep_sources import observed
from test_ep_event_worker import AtomicTransport, config


def enqueue(queue, stage='IDENTITY', symbol='SNOW', revision='revision', now=NOW, payload=None):
    return queue.enqueue(stage, symbol, 'document', revision, payload or {}, now, now + timedelta(hours=24))


def worker(tmp_path, store, **kwargs):
    return WorkerConfig(database=str(store.path), reviews_database=str(tmp_path / 'review.sqlite3'),
                        output_directory=str(tmp_path / 'reports'), key_file=str(tmp_path / 'key'), **kwargs)


def snapshot(symbols, **kwargs):
    rows = [dict(ticker=s, name=s, asset_type='STOCK', exchange='NASDAQ', trading_status='ACTIVE',
                 cik='0000000123', cusip=s, isin=s) for s in symbols]
    return _normalize(pd.DataFrame(rows), observed_at=kwargs.get('observed_at', NOW - timedelta(hours=4)),
                      target_session=kwargs.get('session', '2026-09-02'), source='FMP_BULK_INPUT',
                      version='fixture-v1', now=NOW)


def test_queue_survives_restart_dedup_and_expired_lease(tmp_path):
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    jid = enqueue(q)
    first = q.claim('IDENTITY', NOW, lease_seconds=30)
    assert q.claim('IDENTITY', NOW) is None
    q = PipelineQueue(q.path)
    assert enqueue(q) == jid
    second = q.claim('IDENTITY', NOW + timedelta(seconds=31))
    assert second['attempts'] == 2 and second['lease_token'] != first['lease_token']
    with pytest.raises(ValueError, match='LEASE_LOST'):
        q.finish(first, 'COMPLETE', 'STALE_WORKER', NOW + timedelta(seconds=32))
    q.finish(second, 'COMPLETE', 'DONE', NOW + timedelta(seconds=32))
    assert enqueue(q) == jid and not q.ready('IDENTITY', NOW + timedelta(minutes=1))
    history = q.explain('snow')['jobs'][0]
    assert history['state'] == 'COMPLETE'
    assert len(history['history']) == 4 and 'lease_token' not in history


def test_queue_concurrency_retry_and_expiry(tmp_path):
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    enqueue(q)
    with ThreadPoolExecutor(max_workers=4) as pool:
        leases = list(pool.map(lambda _: PipelineQueue(q.path).claim('IDENTITY', NOW), range(4)))
    leases = [x for x in leases if x]
    assert len(leases) == 1
    q.finish(leases[0], 'RETRY', 'UPSTREAM_UNAVAILABLE', NOW, retry_seconds=60)
    assert not q.ready('IDENTITY', NOW + timedelta(seconds=59))
    assert len(q.ready('IDENTITY', NOW + timedelta(seconds=60))) == 1
    assert q.expire(NOW + timedelta(days=1)) == 1
    assert q.explain('SNOW')['jobs'][0]['state'] == 'EXPIRED'
    assert q.explain('UNKNOWN')['reason'] == 'NOT_DISCOVERED_IN_OBSERVED_FEED_SCOPE'


def test_queue_readonly_cannot_mutate_or_replace_other_database(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    q = PipelineQueue(path)
    enqueue(q)
    before = path.read_bytes()
    ro = PipelineQueue(path, read_only=True)
    ro.summary()
    with pytest.raises(ValueError, match='WRITABLE'):
        ro.claim('IDENTITY', NOW)
    assert path.read_bytes() == before
    store = EpStore(tmp_path / 'evidence.sqlite3')
    with pytest.raises(ValueError, match='NOT_AN_EP_PIPELINE_DATABASE'):
        PipelineQueue(store.path)


def test_bulk_identity_not_refreshed_by_observation_and_ambiguous_rows():
    snap = snapshot(['SNOW'])
    profile = snap.profile('SNOW', NOW)
    assert profile and not snap.profile('SNOW', NOW + timedelta(days=2))
    profile['identity_source_received_at'] = (NOW - timedelta(days=5)).isoformat()
    assert not current_profile({'observed_at': NOW.isoformat(), 'status': 'OK', 'profile': profile}, NOW)
    with pytest.raises(ValueError, match='STALE_OR_FUTURE'):
        snapshot(['SNOW'], observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match='STALE_OR_FUTURE'):
        snapshot(['SNOW'], session='2026-08-31')
    frame = pd.DataFrame([dict(ticker='SNOW', name='Snowflake', asset_type='STOCK', exchange='NASDAQ',
                             trading_status='ACTIVE', cik='123', cusip=cusip, isin='') for cusip in ['A', 'B']])
    frame['asset_type'] = frame['asset_type'].astype('category')
    snap = _normalize(frame, observed_at=NOW, target_session='2026-09-02', source='FMP_BULK_INPUT', version='v', now=NOW)
    assert snap.rejected == {'SNOW': 'AMBIGUOUS_BULK_IDENTITY'} and not snap.profiles


def test_manifest_hash_and_path_are_validated(tmp_path):
    frame = pd.DataFrame([dict(ticker='SNOW', name='Snowflake', asset_type='STOCK', exchange='NASDAQ',
                              trading_status='ACTIVE', cik='123', cusip='', isin='')])
    data = tmp_path / 'profiles.parquet'
    frame.to_parquet(data)
    manifest = {'schema_version': 1, 'source': 'FMP_SECURITY_MASTER_INPUTS', 'created_at': NOW.isoformat(),
                'target_session': '2026-09-02', 'artifacts': {'profiles': {'file': data.name, 'rows': 1,
                'sha256': hashlib.sha256(data.read_bytes()).hexdigest()}}}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    assert from_provider_manifest(path, NOW).profile('SNOW', NOW)
    manifest['artifacts']['profiles']['sha256'] = 'wrong'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        from_provider_manifest(path, NOW)
    manifest['artifacts']['profiles']['file'] = '../other.parquet'
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='PATH_REJECTED'):
        from_provider_manifest(path, NOW)


def test_batch_identity_covers_more_than_twenty_without_profile_requests(tmp_path):
    symbols = ['T' + chr(65 + i // 26) + chr(65 + i % 26) for i in range(65)]
    snap = snapshot(symbols)
    store = EpStore(tmp_path / 'ep.sqlite3')
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    provider = FakeProvider(articles=[article(s) for s in symbols], calendar=[])
    report = EpRadar(store, provider, EpSettings(max_profiles=0), clock=lambda: NOW,
                     identity_snapshot=snap, pipeline=q).collect('2026-09-02', '2026-09-03')
    seed(q, report, NOW)
    result = process_identities(q, store, provider, snap, worker(tmp_path, store), clock=lambda: NOW)
    assert result['counts'] == {'ELIGIBLE_IDENTITY_RESOLVED': 65}
    assert len(q.ready('SOURCE', NOW)) == 65
    assert not any(c[0] == 'profile' for c in provider.calls)
    assert report['summary']['bulk_profiles_used'] == 65


def test_fallback_capacity_is_deferred_and_next_cycle_resumes(tmp_path):
    store = EpStore(tmp_path / 'ep.sqlite3')
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    provider = FakeProvider(articles=[article(s) for s in ['AAA', 'BBB', 'CCC']], calendar=[])
    report = EpRadar(store, provider, EpSettings(max_profiles=0), clock=lambda: NOW).collect('2026-09-02', '2026-09-03')
    seed(q, report, NOW)
    cfg = worker(tmp_path, store, identity_fallback_requests=1)
    result = process_identities(q, store, provider, IdentitySnapshot(NOW), cfg, clock=lambda: NOW)
    assert result['counts']['IDENTITY_REQUEST_CAPACITY_DEFERRED'] == 2
    assert len(q.ready('SOURCE', NOW)) == 1
    result = process_identities(q, store, provider, IdentitySnapshot(NOW), cfg,
                                clock=lambda: NOW + timedelta(minutes=5))
    assert result['counts']['ELIGIBLE_IDENTITY_RESOLVED'] == 1
    assert len(q.ready('SOURCE', NOW + timedelta(minutes=5))) == 2


def test_incremental_tail_progresses_without_restarting_and_retains_pending(tmp_path):
    store = EpStore(tmp_path / 'ep.sqlite3')
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    provider = FakeProvider(articles=[article('S' + chr(65 + i), url=f'https://example.test/{i}') for i in range(9)], calendar=[])
    def collect():
        report = EpRadar(store, provider, EpSettings(max_profiles=0, max_pages=2, page_size=2),
                         clock=lambda: NOW, pipeline=q).collect('2026-09-02', '2026-09-03')
        seed(q, report, NOW)
        return report
    collect()
    assert q.checkpoint('feed:press')['next_page'] == 2
    collect()
    assert q.checkpoint('feed:press')['next_page'] == 3
    collect()
    final = collect()
    assert q.checkpoint('feed:press')['next_page'] == 1
    assert len(q.ready('IDENTITY', NOW)) == 9
    assert [c[1] for c in provider.calls if c[0] == 'press'] == [0, 1, 0, 2, 0, 3, 0, 4]
    assert all(c['coverage_gap_possible'] for c in final['summary']['coverage'] if c['feed'] != 'calendar')


def test_invalid_headline_does_not_pin_incremental_feed_to_page_zero(tmp_path):
    store = EpStore(tmp_path / 'ep.sqlite3')
    queue = PipelineQueue(tmp_path / 'queue.sqlite3')
    rows = [article('AAA'), article('BBB'), article('CCC'), article('DDD'), article('EEE')]
    rows[0]['title'] = ''
    provider = FakeProvider(articles=rows, calendar=[])
    result = EpRadar(store, provider, EpSettings(max_profiles=0, max_pages=2, page_size=2),
                     clock=lambda: NOW, pipeline=queue).collect('2026-09-02', '2026-09-03')
    assert [c[1] for c in provider.calls if c[0] == 'press'] == [0, 1]
    assert queue.checkpoint('feed:press')['next_page'] == 2
    coverage = next(c for c in result['summary']['coverage'] if c['feed'] == 'press')
    assert coverage['invalid_rows'] == 1 and coverage['status'] == 'PARTIAL_INVALID_RECORDS'
    assert not coverage['sweep_complete']


def test_source_fetch_slots_prioritize_event_hints_without_starving_other_news(tmp_path):
    from src.breakouts.ep.pipeline import select_source_jobs
    queue = PipelineQueue(tmp_path / 'queue.sqlite3')
    for i in range(16):
        queue.enqueue('SOURCE', 'T' + chr(65 + i), str(i), 'v1', {'event': {
            'document_id': str(i), 'revision_id': 'v1', 'published_at': NOW.isoformat(),
            'evidence': {'title': 'Business update'},
            'event_type_hint': 'UNKNOWN' if i < 8 else 'EARNINGS'}},
            NOW + timedelta(seconds=i), NOW + timedelta(days=1))
    selected = select_source_jobs(queue, NOW + timedelta(minutes=1), 8)
    assert len(selected) == 8
    assert sum(r['payload']['event']['event_type_hint'] == 'EARNINGS' for r in selected) == 6
    assert sum(r['payload']['event']['event_type_hint'] == 'UNKNOWN' for r in selected) == 2
    assert len(queue.ready('SOURCE', NOW + timedelta(minutes=1))) == 16


def test_completed_analysis_handoff_recovery_is_idempotent_and_respects_expiry(tmp_path):
    queue = PipelineQueue(tmp_path / 'queue.sqlite3')
    queue.enqueue('ANALYSIS', 'TEST', 'doc', 'v1', {'source_id': 's'}, NOW, NOW + timedelta(hours=1))
    lease = queue.claim('ANALYSIS', NOW)
    queue.finish(lease, 'COMPLETE', 'AI_PROPOSAL_VALIDATED_NOT_FACT_APPROVED', NOW,
                 result={'request_key': 'key', 'delivery': 'DISABLED'})
    assert queue.recover_delivery_handoffs(NOW + timedelta(seconds=1)) == 1
    assert queue.recover_delivery_handoffs(NOW + timedelta(seconds=2)) == 0
    assert queue.ready('DELIVERY', NOW + timedelta(seconds=2))[0]['payload']['request_key'] == 'key'
    queue.enqueue('ANALYSIS', 'TEST', 'old', 'v1', {'source_id': 'old'}, NOW, NOW + timedelta(seconds=5))
    lease = queue.claim('ANALYSIS', NOW)
    queue.finish(lease, 'COMPLETE', 'VALIDATED', NOW, result={'request_key': 'old'})
    assert queue.recover_delivery_handoffs(NOW + timedelta(seconds=6)) == 0


def test_analysis_queue_readonly_execution_and_no_repeat_charge(tmp_path, observed, monkeypatch):
    store, _, sid = seeded(observed)
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    cfg = config(tmp_path, store, sid).model_copy(update={'jobs': [], 'queue_database': str(q.path)})
    enqueue(q, stage='ANALYSIS', payload={'source_id': sid}, now=CLOCK())
    monkeypatch.setattr('src.breakouts.ep.event_worker.source_brief',
                        lambda *a: {'freshness': {'status': 'CURRENT_WINDOW_DATE_ONLY'}})
    result = cycle(cfg, clock=CLOCK, key_reader=lambda *a: pytest.fail('read key'))
    assert result['external_requests'] == 0 and q.ready('ANALYSIS', CLOCK())[0]['attempts'] == 0
    transport = AtomicTransport()
    result = cycle(cfg, execute=True, clock=CLOCK, transport_factory=lambda *a: transport, key_reader=lambda *a: 'test')
    assert result['external_requests'] == 1
    assert q.explain('SNOW')['jobs'][0]['state'] == 'COMPLETE'
    again = cycle(cfg, execute=True, clock=CLOCK, key_reader=lambda *a: pytest.fail('duplicate charge'))
    assert again['external_requests'] == 0 and not again['items']


def test_analysis_queue_uncertain_reservation_blocked_not_retried(tmp_path, observed, monkeypatch):
    from src.breakouts.ep.event_worker import select_jobs, settings
    from src.breakouts.ep.llm_service import plan_llm
    store, _, sid = seeded(observed)
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    cfg = config(tmp_path, store, sid).model_copy(update={'jobs': [], 'queue_database': str(q.path)})
    enqueue(q, stage='ANALYSIS', payload={'source_id': sid}, now=CLOCK())
    monkeypatch.setattr('src.breakouts.ep.event_worker.source_brief',
                        lambda *a: {'freshness': {'status': 'CURRENT_WINDOW_DATE_ONLY'}})
    jobs, _ = select_jobs(store, cfg, CLOCK())
    plan = plan_llm(store, sid, settings(True), protocol='event-claims', paragraph_ids=jobs[0].paragraph_ids, as_of=CLOCK())
    store.reserve_llm_call(plan['request_key'], sid, {}, 1, settings(True), CLOCK())
    result = cycle(cfg, execute=True, clock=CLOCK, key_reader=lambda *a: pytest.fail('retry charge'))
    assert result['external_requests'] == 0
    assert q.explain('SNOW')['jobs'][0]['reason'] == 'RESERVED'
    assert q.explain('SNOW')['jobs'][0]['state'] == 'BLOCKED'


def test_source_outage_keeps_job_and_reason(tmp_path, observed):
    store, report = observed
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    event = report['candidates'][0]['events'][0]
    enqueue(q, stage='SOURCE', payload={'run_id': report['run_id'], 'event': event})
    class Resolver:
        registry = {'issuers': {}}
        def resolve_event(self, *args):
            return {'status': 'SOURCE_HTTP_429', 'source_id': 'receipt'}
    result = process_sources(q, store, Resolver(), worker(tmp_path, store), clock=CLOCK)
    assert result == {'SOURCE_HTTP_429': 1}
    assert q.explain('SNOW')['jobs'][0]['state'] == 'RETRY'


def test_ambiguous_bulk_identity_invalidates_cached_good_profile(tmp_path):
    store = EpStore(tmp_path / 'ep.sqlite3')
    provider = FakeProvider(calendar=[])
    EpRadar(store, provider, clock=lambda: NOW).collect('2026-09-02', '2026-09-03')
    snap = IdentitySnapshot(NOW, rejected={'SNOW': 'AMBIGUOUS_BULK_IDENTITY'})
    report = EpRadar(store, provider, clock=lambda: NOW + timedelta(seconds=1), identity_snapshot=snap).collect(
        '2026-09-02', '2026-09-03')
    assert report['candidates'][0]['identity'] is None
    assert report['candidates'][0]['identity_status'] == 'AMBIGUOUS_BULK_IDENTITY'
    assert not current_profile(store.profiles(NOW + timedelta(seconds=1))['SNOW'], NOW + timedelta(seconds=1))


def test_sec_index_failure_does_not_prevent_discovery_or_queue(tmp_path, monkeypatch):
    from src.breakouts.ep.event_ingest import ingest
    from src.data.public_articles import SourceAccessError
    class Index:
        requests = 0
        def fetch_json(self, url):
            raise SourceAccessError('ROBOTS_DISALLOWED')
    class NoIssuerClient:
        requests = 0
    monkeypatch.delenv('SEC_CONTACT_EMAIL', raising=False)
    store = EpStore(tmp_path / 'ep.sqlite3')
    cfg = worker(tmp_path, store, collect_enabled=True)
    result = ingest(cfg, clock=lambda: NOW, provider=FakeProvider(calendar=[]), index_client=Index(),
                    disclosure_client=NoIssuerClient(), identity_snapshot=snapshot(['SNOW']))
    assert result['candidate_count'] == 1 and result['registered_candidates'] == 0
    assert result['identity_processing']['counts']['ELIGIBLE_IDENTITY_RESOLVED'] == 1
    assert result['index_status'] == 'COMPANY_INDEX_NOT_AVAILABLE'
    assert result['source_counts'] == {'ISSUER_NOT_REGISTERED': 1}


def test_failed_incremental_page_does_not_advance_cursor(tmp_path):
    store = EpStore(tmp_path / 'ep.sqlite3')
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    class Provider(FakeProvider):
        broken = True
        def articles(self, feed, page, limit, *, timeout):
            if self.broken and feed == 'press' and page == 1:
                raise TimeoutError()
            return super().articles(feed, page, limit, timeout=timeout)
    provider = Provider(articles=[article('A' + chr(65 + i)) for i in range(5)], calendar=[])
    def collect():
        return EpRadar(store, provider, EpSettings(max_profiles=0, max_pages=2, page_size=2),
                       clock=lambda: NOW, pipeline=q).collect('2026-09-02', '2026-09-03')
    collect()
    assert q.checkpoint('feed:press')['next_page'] == 1
    provider.broken = False
    collect()
    assert q.checkpoint('feed:press')['next_page'] == 2


def test_completed_analysis_survives_crash_before_outbox_handoff(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from src.alerts.ep_event import notifications, EventOutbox
    from test_ep_event_delivery import report, Sender
    store = EpStore(tmp_path / 'ep.sqlite3')
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    cfg = worker(tmp_path, store, queue_database=str(q.path), enabled=True, delivery_enabled=True,
                 allow_unreviewed_ai=True, outbox_database=str(tmp_path / 'outbox.sqlite3'),
                 webhook_file=str(tmp_path / 'webhook'), expected_channel_id='123')
    enqueue(q, stage='DELIVERY', now=datetime.now(timezone.utc), payload={'request_key': 'doc'}, symbol='TEST')
    monkeypatch.setattr('src.breakouts.ep.event_workflow.reviewed_report', lambda *a: report())
    monkeypatch.setattr('src.breakouts.ep.event_worker.private_text', lambda *a: 'redacted')
    sender = Sender()
    result = notifications(cfg, {'items': []}, sender_factory=lambda *a: sender)
    assert result['outbox'] == {'SENT': 1}
    job = q.explain('TEST')['jobs'][0]
    assert job['state'] == 'COMPLETE' and job['result']['outbox_id']
    notifications(cfg, {'items': []}, sender_factory=lambda *a: pytest.fail('duplicate delivery'))
    assert len(sender.calls) == 1 and EventOutbox(cfg.outbox_database).status() == {'SENT': 1}


def test_cli_explain_read_only_and_no_secret_required(tmp_path):
    import subprocess
    import sys
    from test_ep_radar import ROOT
    store = EpStore(tmp_path / 'ep.sqlite3')
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    enqueue(q)
    cfg = worker(tmp_path, store, queue_database=str(q.path))
    path = tmp_path / 'config.json'
    path.write_text(cfg.model_dump_json())
    before = q.path.read_bytes()
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/run_ep_event_worker.py'), '--config', str(path),
                             'explain', 'SNOW'], capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload['ticker'] == 'SNOW' and payload['external_requests'] == 0
    assert payload['jobs'][0]['state'] == 'PENDING'
    assert q.path.read_bytes() == before


def test_syndicated_news_shares_one_official_text_analysis(tmp_path, observed):
    store, report = observed
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    event = report['candidates'][0]['events'][0]
    for revision in ['wire-one', 'wire-two']:
        enqueue(q, stage='SOURCE', revision=revision, payload={'run_id': report['run_id'], 'event': event})
    class Resolver:
        registry = {'issuers': {}}
        def resolve_event(self, *args):
            return {'status': 'DOCUMENT_MATCHED', 'source_id': 'receipt', 'text_revision': 'body-revision',
                    'final_url': 'https://www.sec.gov/Archives/edgar/data/1/000000000000000001/release.htm'}
    counts = process_sources(q, store, Resolver(), worker(tmp_path, store), clock=CLOCK)
    assert counts == {'DOCUMENT_MATCHED': 1}
    assert len(q.ready('ANALYSIS', CLOCK())) == 1
    assert sum(j['state'] == 'COMPLETE' for j in q.explain('SNOW')['jobs']) == 3


def test_default_queue_cannot_collide_with_other_worker_paths(tmp_path):
    store = EpStore(tmp_path / 'ep.sqlite3')
    with pytest.raises(ValueError, match='DISTINCT_ABSOLUTE'):
        WorkerConfig(database=str(store.path), reviews_database=str(store.path) + '.pipeline.sqlite3',
                     output_directory=str(tmp_path / 'reports'), key_file=str(tmp_path / 'key'))


def test_worker_expires_stale_tasks_without_reading_key(tmp_path, observed):
    store, _, sid = seeded(observed)
    q = PipelineQueue(tmp_path / 'queue.sqlite3')
    enqueue(q, stage='ANALYSIS', now=CLOCK() - timedelta(days=2), payload={'source_id': sid})
    cfg = config(tmp_path, store, sid).model_copy(update={'jobs': [], 'queue_database': str(q.path)})
    result = cycle(cfg, execute=True, clock=CLOCK, key_reader=lambda *a: pytest.fail('expired task read key'))
    assert result['external_requests'] == 0 and q.explain('SNOW')['jobs'][0]['state'] == 'EXPIRED'
