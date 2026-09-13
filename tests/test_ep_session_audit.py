from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from src.breakouts.ep import latency
from src.breakouts.ep.models import timestamp
from src.breakouts.ep.price_discovery import PriceStore
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.session_audit import report, consumer_coverage


NOW = datetime(2026, 9, 11, 12, 15, tzinfo=timezone.utc)
SESSION = '2026-09-11'


def setup(tmp_path):
    return PipelineQueue(tmp_path / 'queue.sqlite3'), PriceStore(tmp_path / 'prices.sqlite3')


def price(store, symbol, at=NOW, status='PRICE_WATCH', reason='PENDING_ACCEPTANCE'):
    scan = symbol + timestamp(at)
    store.save(scan, [{'receipt_id': scan, 'ticker': symbol, 'observed_at': timestamp(at),
        'status': status, 'reason': reason, 'price_at': timestamp(at - timedelta(seconds=5)),
        'indicative_gap_pct': 8 if status == 'PRICE_WATCH' else 1}],
        {'started_at': timestamp(at), 'finished_at': timestamp(at), 'status': 'PARTIAL_SWEEP'})


def analysis(queue, symbol, name, at=NOW):
    job = queue.enqueue('ANALYSIS', symbol, name, 'v1', {}, at, NOW + timedelta(hours=8))
    latency.record(queue, symbol, SESSION, 'SOURCE_MATCHED', name, at,
        {'event_id': name, 'source_job_id': 'source-' + name, 'analysis_job_id': job,
         'news_first_seen_at': timestamp(at - timedelta(seconds=20))})
    return job


def audit(q, p, at=NOW, symbols=('ORCL', 'CPRT', 'RH')):
    return report(PipelineQueue(q.path, read_only=True), p.path, SESSION, at,
                  symbols=symbols, captured_at=NOW + timedelta(days=1))


def test_future_price_and_completion_do_not_backfill_history(tmp_path):
    q, p = setup(tmp_path)
    price(p, 'ORCL')
    job = analysis(q, 'ORCL', 'earnings')
    lease = q.claim('ANALYSIS', NOW + timedelta(seconds=10), job_id=job)
    q.finish(lease, 'COMPLETE', 'AI_PROPOSAL_VALIDATED_NOT_FACT_APPROVED', NOW + timedelta(seconds=30))
    price(p, 'ORCL', NOW + timedelta(seconds=40), status='BELOW_THRESHOLD')
    before = audit(q, p, NOW + timedelta(seconds=20))['symbols'][1]
    assert before['ticker'] == 'ORCL'
    assert before['price']['status'] == 'PRICE_WATCH'
    assert before['events'][0]['analysis_state'] == 'RUNNING'
    assert before['events'][0]['analysis_completed_at'] is None
    after = audit(q, p, NOW + timedelta(seconds=50))['symbols'][1]
    assert after['price']['status'] == 'BELOW_THRESHOLD'
    assert after['price']['first_watch_at'] == timestamp(NOW)
    assert after['events'][0]['analysis_elapsed_seconds'] == 20
    assert after['events'][0]['price_to_analysis_seconds'] == 30


def test_shared_ticker_does_not_join_unrelated_analysis(tmp_path):
    q, p = setup(tmp_path)
    first = analysis(q, 'ORCL', 'earnings')
    second = analysis(q, 'ORCL', 'acquisition')
    lease = q.claim('ANALYSIS', NOW, job_id=second)
    q.finish(lease, 'COMPLETE', 'AI_PROPOSAL_VALIDATED_NOT_FACT_APPROVED', NOW + timedelta(seconds=5))
    items = {e['analysis_job_id']: e for e in audit(q, p, NOW + timedelta(seconds=10))['symbols'][1]['events']}
    assert items[first]['analysis_completed_at'] is None
    assert items[second]['analysis_completed_at'] == timestamp(NOW + timedelta(seconds=5))


def test_other_article_exclusion_does_not_veto_matched_event(tmp_path):
    q, p = setup(tmp_path)
    price(p, 'ORCL')
    analysis(q, 'ORCL', 'earnings')
    job = q.enqueue('IDENTITY', 'ORCL', 'legal-notice', 'v1', {}, NOW, NOW + timedelta(hours=1))
    lease = q.claim('IDENTITY', NOW, job_id=job)
    q.finish(lease, 'EXCLUDED', 'LEGAL_NOTICE', NOW)
    item = audit(q, p)['symbols'][1]
    assert 'IDENTITY:EXCLUDED:LEGAL_NOTICE' not in item['reasons']
    assert any(j['reason'] == 'LEGAL_NOTICE' for j in item['queue_jobs'])
    assert 'ANALYSIS:PENDING:QUEUED' in item['reasons']


def test_empty_price_db_is_unobserved_not_failed_recall(tmp_path):
    q, p = setup(tmp_path)
    result = audit(q, p)
    assert result['status'] == 'NO_SESSION_OBSERVATIONS'
    assert result['market_recall'] is None and not result['strategy_accepted']
    assert all('PRICE_NOT_OBSERVED_IN_CAPTURED_SCOPE' in s['reasons'] for s in result['symbols'])
    assert result['external_requests'] == result['llm_requests'] == result['discord_messages'] == 0


def test_price_rejection_queue_wait_and_source_failure_remain_distinct(tmp_path):
    q, p = setup(tmp_path)
    price(p, 'ORCL', status='BELOW_THRESHOLD')
    price(p, 'CPRT', status='BLOCKED', reason='PREVIOUS_SESSION_BASELINE_UNAVAILABLE')
    price(p, 'RH')
    q.enqueue('NEWS', 'RH', 'news', 'v1', {'session': SESSION}, NOW, NOW + timedelta(hours=1))
    sid = q.enqueue('SOURCE', 'RH', 'source', 'v1', {}, NOW, NOW + timedelta(hours=1))
    lease = q.claim('SOURCE', NOW, job_id=sid)
    q.finish(lease, 'RETRY', 'OFFICIAL_DOCUMENT_UNAVAILABLE', NOW)
    by = {i['ticker']: i for i in audit(q, p)['symbols']}
    assert 'PRICE_BELOW_THRESHOLD' in by['ORCL']['reasons']
    assert 'PRICE_INPUT_BLOCKED:PREVIOUS_SESSION_BASELINE_UNAVAILABLE' in by['CPRT']['reasons']
    assert 'NEWS:PENDING:QUEUED' in by['RH']['reasons']
    assert 'SOURCE:RETRY:OFFICIAL_DOCUMENT_UNAVAILABLE' in by['RH']['reasons']


def test_grouped_news_complete_is_not_original_or_analysis_success(tmp_path):
    q, p = setup(tmp_path)
    sid = q.enqueue('SOURCE', 'ORCL', 'legacy', 'v1', {}, NOW, NOW + timedelta(hours=1))
    lease = q.claim('SOURCE', NOW, job_id=sid)
    q.finish(lease, 'COMPLETE', 'GROUPED_INTO_EVENT_NOT_SOURCE_SUCCESS', NOW)
    item = audit(q, p)['symbols'][1]
    assert item['events'] == []
    assert 'NO_SOURCE_MATCH_CLOCK_IN_THIS_SESSION' in item['reasons']
    assert item['queue_jobs'][0]['reason'] == 'GROUPED_INTO_EVENT_NOT_SOURCE_SUCCESS'


def test_future_session_waits_and_no_input_is_changed(tmp_path):
    q, p = setup(tmp_path)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.suffix == '.sqlite3'}
    result = report(PipelineQueue(q.path, read_only=True), p.path, '2026-09-14', NOW, captured_at=NOW)
    assert result['status'] == 'WAITING_FOR_SESSION'
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.suffix == '.sqlite3'}
    with pytest.raises(ValueError, match='ASOF_CANNOT'):
        report(q, p.path, SESSION, NOW + timedelta(seconds=1), captured_at=NOW)
    with pytest.raises(ValueError):
        report(q, p.path, SESSION, NOW.replace(tzinfo=None))


def test_pending_age_visible_when_completed_latency_sample_is_empty(tmp_path):
    q, p = setup(tmp_path)
    price(p, 'ORCL')
    analysis(q, 'ORCL', 'earnings')
    result = audit(q, p, NOW + timedelta(minutes=10))
    assert result['metrics']['analysis_elapsed_seconds']['observed_count'] == 0
    assert result['symbols'][1]['queue_jobs'][0]['age_seconds'] == 600
    assert result['symbols'][1]['events'][0]['analysis_queue_wait_seconds'] is None


def test_scan_completed_after_cutoff_is_not_historical_completion(tmp_path):
    q, p = setup(tmp_path)
    price(p, 'ORCL')
    with sqlite3.connect(p.path) as db:
        db.execute("UPDATE ep_price_scans SET payload=json_set(payload,'$.finished_at',?)", (timestamp(NOW + timedelta(minutes=1)),))
    result = audit(q, p)
    assert result['price_scanned_symbols'] == 1
    assert result['completed_scan_records'] == 0


def test_later_session_prices_are_not_included_in_prior_day_report(tmp_path):
    q, p = setup(tmp_path)
    price(p, 'ORCL', NOW + timedelta(days=3))
    result = report(q, p.path, SESSION, NOW + timedelta(days=4), symbols=['ORCL'], captured_at=NOW + timedelta(days=4))
    assert result['price_scanned_symbols'] == 0
    assert result['symbols'][0]['price_discovered_at'] is None


def test_latency_receipt_retained_if_price_database_is_unavailable(tmp_path):
    q, p = setup(tmp_path)
    latency.record(q, 'ORCL', SESSION, 'PRICE_DISCOVERED', SESSION, NOW, {'price_at': timestamp(NOW)})
    result = report(q, None, SESSION, NOW, captured_at=NOW)
    assert result['symbols'][0]['price_discovered_at'] == timestamp(NOW)
    assert result['price_scope'] == 'PRICE_DATABASE_NOT_INITIALIZED'


def test_news_receipt_is_visible_before_original_but_not_before_event_attachment(tmp_path):
    q, p = setup(tmp_path)
    q.enqueue('SOURCE', 'ORCL', 'event', 'event-v1', {}, NOW, NOW + timedelta(hours=1))
    payload = {'event': {'document_id': 'news1', 'revision_id': 'rev1', 'first_seen_at': timestamp(NOW),
                         'published_at': timestamp(NOW - timedelta(hours=1))}}
    with q.connection() as db:
        db.execute('INSERT INTO events VALUES(?,?,?,?)', ('event', 'ORCL', '{}', timestamp(NOW)))
        db.execute('INSERT INTO event_members VALUES(?,?,?,?,?)',
                   ('event', 'news1', 'rev1', timestamp(NOW + timedelta(seconds=3)), json.dumps(payload)))
    early = audit(q, p, NOW + timedelta(seconds=2))['symbols'][1]
    later = audit(q, p, NOW + timedelta(seconds=4))['symbols'][1]
    assert early['evidence_members'] == []
    assert later['evidence_members'][0]['news_first_seen_at'] == timestamp(NOW)
    assert later['events'] == []


def test_readonly_legacy_clock_table_is_not_created(tmp_path):
    q, p = setup(tmp_path)
    with q.connection() as db:
        db.execute('DROP TABLE latency_events')
    assert audit(q, p)['status'] == 'NO_SESSION_OBSERVATIONS'
    with q.connection() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='latency_events'").fetchone() is None


@pytest.mark.parametrize('session,close,expected', [('2026-09-11', '2026-09-11T20:00:00+00:00', 720),
                                                 ('2026-11-27', '2026-11-27T18:00:00+00:00', 540)])
def test_consumer_schedule_excludes_close_and_handles_early_close(tmp_path, session, close, expected):
    result = consumer_coverage(tmp_path, session, datetime.fromisoformat(close))
    assert all(r['expected_schedule_slots'] == expected for r in result.values())
    assert all(r['completed_records'] == 0 for r in result.values())


def test_consumer_history_never_reads_future_latest_checkpoint(tmp_path):
    folder = tmp_path / 'consumers' / 'news'
    folder.mkdir(parents=True)
    value = {'started_at': timestamp(NOW), 'finished_at': timestamp(NOW + timedelta(seconds=20)),
             'status': 'COMPLETED', 'elapsed_seconds': 20, 'after': []}
    (folder / (timestamp(NOW).replace(':', '-') + '.json')).write_text(json.dumps(value))
    (folder / 'latest.json').write_text(json.dumps({**value, 'status': 'FAILED'}))
    assert consumer_coverage(tmp_path, SESSION, NOW)['news']['completed_records'] == 0
    after = consumer_coverage(tmp_path, SESSION, NOW + timedelta(seconds=30))['news']
    assert after['status_counts'] == {'COMPLETED': 1}
    assert after['elapsed_p95_seconds'] == 20


def test_cli_future_checkpoints_wait_without_model_or_notifications(tmp_path, monkeypatch, capsys):
    from src.breakouts.ep.event_worker import WorkerConfig
    q, p = setup(tmp_path)
    config = WorkerConfig(database=str(tmp_path / 'unused.sqlite3'), queue_database=str(q.path),
        price_database=str(p.path), reviews_database=str(tmp_path / 'reviews.sqlite3'),
        output_directory=str(tmp_path / 'reports'), key_file=str(tmp_path / 'unread.key'))
    config_path = tmp_path / 'config.json'
    config_path.write_text(config.model_dump_json())
    script = Path(__file__).resolve().parents[1] / 'scripts/audit_ep_session.py'
    spec = importlib.util.spec_from_file_location('audit_cli', script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(cli, 'datetime', Clock)
    monkeypatch.setattr(sys, 'argv', ['audit', '--config', str(config_path), '--session', '2026-09-14'])
    assert cli.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result['current']['status'] == 'WAITING_FOR_SESSION'
    assert all(r['status'] == 'WAITING_FOR_CHECKPOINT' for r in result['checkpoints'])
    assert not (tmp_path / 'unused.sqlite3').exists()
