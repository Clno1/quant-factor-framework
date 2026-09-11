from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.watch import record_watch, evaluate_watch, source_priority, ingest_watch_file
from src.breakouts.ep.pipeline import select_source_jobs
from src.breakouts.ep.catalyst import freshness, current_commentary_window
from test_ep_analysis import source, DATELINE

NOW = datetime(2026, 9, 11, 12, 15, tzinfo=timezone.utc)


def payload(doc, title='RH Q2 Earnings Call Highlights', text='Second quarter fiscal 2026 results', hint='EARNINGS'):
    return {'run_id': 'run', 'event': {'document_id': doc, 'revision_id': 'v1',
        'published_at': (NOW - timedelta(hours=12)).isoformat(), 'event_type_hint': hint,
        'evidence': {'title': title, 'text': text, 'url': 'https://example.test/' + doc}}}


def observation(symbol='RH', **changes):
    return {'ticker': symbol, 'session': '2026-09-11', 'price': 107, 'previous_close': 100,
            'previous_session': '2026-09-10', 'price_at': NOW.isoformat(), 'observed_at': NOW.isoformat(),
            'price_contract_verified': True, 'adjustment_verified': True, 'evidence_ids': ['receipt:1'],
            'security_identity_verified': True, 'asset_type': 'STOCK', **changes}


def test_event_group_preserves_twenty_nine_articles_and_separates_ma(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    for i in range(29):
        q.enqueue_event('RH', payload(str(i)), NOW, NOW + timedelta(hours=12))
    q.enqueue_event('RH', payload('acquire', 'RH to acquire Example', '', 'M_AND_A'), NOW, NOW + timedelta(hours=12))
    q.enqueue_event('RH', payload('q1', 'RH Q1 Earnings', 'First quarter fiscal 2026 results'), NOW, NOW + timedelta(hours=12))
    jobs = q.ready('SOURCE', NOW)
    assert len(jobs) == 3
    groups = q.explain('RH')['events']
    assert sorted(len(e['members']) for e in groups) == [1, 1, 29]
    assert sum(len(q.event_members(e['event_id'], NOW - timedelta(seconds=1))) for e in groups) == 0
    q = PipelineQueue(q.path)
    q.enqueue_event('RH', payload('1'), NOW, NOW + timedelta(hours=12))
    assert len(q.ready('SOURCE', NOW)) == 3


def test_unbound_quarters_contracts_and_targets_are_not_speculatively_merged(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    for i, title in enumerate(['RH acquires A', 'RH acquires B', 'RH wins contract A', 'RH wins contract B']):
        q.enqueue_event('RH', payload(str(i), title, '', 'UNKNOWN'), NOW, NOW + timedelta(hours=12))
    assert len(q.ready('SOURCE', NOW)) == 4


def test_queue_v1_upgrade_retains_history_and_groups_without_fetch(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    for i in range(2):
        p = payload(str(i))
        q.enqueue('SOURCE', 'RH', str(i), 'v1', p, NOW, NOW + timedelta(hours=12))
    with q.connection() as db:
        db.execute('UPDATE ep_pipeline_schema SET version=1')
        db.execute('DROP TABLE event_members')
        db.execute('DROP TABLE events')
        db.execute('DROP TABLE watch_history')
    ro = PipelineQueue(q.path, read_only=True)
    assert not ro.event_schema and len(ro.explain('RH')['jobs']) == 2
    q = PipelineQueue(q.path)
    assert q.consolidate_sources(NOW) == 2
    assert q.consolidate_sources(NOW) == 0
    assert len(q.ready('SOURCE', NOW)) == 1
    assert len(q.explain('RH')['jobs']) == 3


def test_market_watch_without_news_or_volume_keeps_unknowns(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    result = record_watch(q, observation('QCOM', premarket_rvol=999), NOW)
    assert result['status'] == 'WATCH' and result['catalyst_status'] == 'UNKNOWN'
    assert result['premarket_rvol'] is None and not result['eligible_for_rating']
    assert q.explain('QCOM')['market']['status'] == 'WATCH'
    assert source_priority(q, 'QCOM', NOW) > 0
    assert source_priority(q, 'QCOM', NOW + timedelta(minutes=3)) == 0
    assert not record_watch(q, observation('QCOM', premarket_rvol=999), NOW)['changed']
    assert evaluate_watch(observation(price=104.3), NOW)['status'] == 'WATCH'
    assert evaluate_watch(observation(asset_type='ETF'), NOW)['status'] == 'BLOCKED'
    assert evaluate_watch(observation(price_at=(NOW + timedelta(seconds=1)).isoformat()), NOW)['status'] == 'BLOCKED'


def test_watch_incremental_updates_accumulate_small_moves_and_record_fade(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    first = observation(price=108)
    record_watch(q, first, NOW)
    def step(seconds, price, **kw):
        at = NOW + timedelta(seconds=seconds)
        return record_watch(q, observation(price=price, price_at=at.isoformat(), observed_at=at.isoformat(), **kw), at)
    assert not step(20, 109)['changed']
    assert step(40, 111)['change_reason'] == 'GAP_EXPANDED'
    assert step(60, 107)['change_reason'] == 'GAP_FADED'
    assert step(80, 107, catalyst_status='SOURCE_MATCHED', catalyst_evidence_ids=['source:1'])['change_reason'] == 'CATALYST_STATUS_CHANGED'
    assert step(100, 103)['change_reason'] == 'STATUS_CHANGED'
    assert record_watch(q, first, NOW + timedelta(seconds=120))['duplicate']
    assert q.checkpoint('market:RH')['status'] == 'BELOW_THRESHOLD'


def test_unknown_catalyst_does_not_block_priority_or_reserved_retry(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    for i in range(20):
        q.enqueue_event('S' + chr(65 + i), payload(str(i)), NOW, NOW + timedelta(hours=1))
    retry = q.claim('SOURCE', NOW)
    q.finish(retry, 'RETRY', 'SOURCE_TIMEOUT', NOW, retry_seconds=1)
    at = NOW + timedelta(seconds=2)
    q.enqueue_event('QCOM', payload('watch'), at, at + timedelta(hours=1))
    record_watch(q, observation('QCOM'), at)
    selected = select_source_jobs(q, at, 8)
    assert any(r['ticker'] == 'QCOM' for r in selected)
    assert any(r['job_id'] == retry['job_id'] for r in selected)
    assert len({r['job_id'] for r in selected}) == 8


def test_bad_market_row_cannot_stop_other_snapshots(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    path = tmp_path / 'watch.json'
    path.write_text(json.dumps({'version': 'ep-price-watch-v1', 'observations': [{}, observation()]}))
    report = ingest_watch_file(q, path, NOW)
    assert [r['status'] for r in report['items']] == ['BLOCKED', 'WATCH']
    assert report['external_requests'] == report['discord_messages'] == 0


def test_followup_news_does_not_rejuvenate_old_filing():
    src = source(DATELINE)
    # Fixture announcement is Sep 2; subsequent Sep 3 news remains overnight commentary only.
    src['published_at'] = '2026-09-03T10:00:00+00:00'
    src['result'].update(issuer_linkage='REGISTERED_CIK_AND_CURRENT_SEC_TICKER_MATCH',
        filing_accepted_at='2026-09-02T20:05:00Z', verification={'status': 'DOCUMENT_MATCHED'})
    at = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    result = freshness(src, at)
    assert current_commentary_window(result) and result['followup_news_after_filing']
    assert not result['exact_release_time_verified']
    src['published_at'] = '2026-09-08T10:00:00+00:00'
    assert not current_commentary_window(freshness(src, datetime(2026, 9, 8, 12, tzinfo=timezone.utc)))


def test_timeline_does_not_backfill_later_source_success(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    q.enqueue_event('RH', payload('a'), NOW, NOW + timedelta(hours=1))
    lease = q.claim('SOURCE', NOW + timedelta(minutes=1))
    q.finish(lease, 'COMPLETE', 'ORIGINAL_TEXT_MATCHED', NOW + timedelta(minutes=2))
    assert q.timeline('RH', NOW - timedelta(seconds=1))['reason'] == 'NO_HISTORY_AT_ASOF'
    assert q.timeline('RH', NOW)['jobs'][0]['state'] == 'PENDING'
    assert q.timeline('RH', NOW + timedelta(minutes=1))['jobs'][0]['state'] == 'RUNNING'
    assert q.timeline('RH', NOW + timedelta(minutes=2))['jobs'][0]['age_seconds'] == 120
    record_watch(q, observation(), NOW + timedelta(seconds=60))
    assert q.timeline('RH', NOW)['watch_history'] == []
    assert len(q.timeline('RH', NOW + timedelta(seconds=60))['watch_history']) == 1


def test_article_revision_reopens_source_without_repeating_same_revision(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    p = payload('a')
    jid = q.enqueue_event('RH', p, NOW, NOW + timedelta(hours=1))
    lease = q.claim('SOURCE', NOW)
    q.finish(lease, 'COMPLETE', 'ORIGINAL_TEXT_MATCHED', NOW)
    revised = deepcopy(p)
    revised['event']['revision_id'] = 'v2'
    q.enqueue_event('RH', revised, NOW + timedelta(seconds=1), NOW + timedelta(hours=1))
    jobs = q.ready('SOURCE', NOW + timedelta(seconds=1))
    assert len(jobs) == 1 and jobs[0]['reason'] == 'SOURCE_REVISION_RECHECK'
    lease = q.claim('SOURCE', NOW + timedelta(seconds=1))
    revised['event']['revision_id'] = 'v3'
    q.enqueue_event('RH', revised, NOW + timedelta(seconds=2), NOW + timedelta(hours=1))
    q.finish(lease, 'COMPLETE', 'ORIGINAL_TEXT_MATCHED', NOW + timedelta(seconds=3))
    job = next(j for j in q.explain('RH')['jobs'] if j['job_id'] == jid)
    assert job['reason'] == 'EVENT_REVISED_DURING_SOURCE_FETCH'
    at = NOW + timedelta(minutes=6)
    lease = q.claim('SOURCE', at)
    q.finish(lease, 'COMPLETE', 'ORIGINAL_TEXT_MATCHED', at)
    q.enqueue_event('RH', revised, at, NOW + timedelta(hours=1))
    assert not q.ready('SOURCE', at)
