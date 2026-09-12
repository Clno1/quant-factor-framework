from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import json

import pytest

from src.breakouts.ep.identity import IdentitySnapshot
from src.breakouts.ep.price_discovery import PriceStore, discover, inspect_price, price_status
from src.breakouts.ep.price_news import collect_price_news
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.store import EpStore
from src.breakouts.ep.watch import source_priority
from src.breakouts.ep import latency
from src.breakouts.ep.models import timestamp

NOW = datetime(2026, 9, 11, 12, 15, tzinfo=timezone.utc)
SESSION = '2026-09-11'
BASE = {'close': 100, 'session': '2026-09-10', 'contract_id': 'contract', 'version_id': 'version'}


def config(**kwargs):
    return SimpleNamespace(**{'price_batches_per_cycle': 1, 'price_deadline_seconds': 60,
        'price_gap_threshold': 4, 'price_news_jobs': 2, **kwargs})


def identities(symbols):
    return IdentitySnapshot(NOW, {s: {'ticker': s, 'name': s, 'asset_type': 'STOCK',
        'exchange': 'NASDAQ', 'is_actively_trading': True} for s in symbols}, provenance={'version': 'one'})


def quote(symbol='ORCL', now=NOW, price=108, premarket=True):
    return {'symbol': symbol, 'price': price, 'timestamp': now.timestamp() * (1000 if premarket else 1)}


class Prices:
    def __init__(self):
        self.calls = []
        self.price = 108

    def prices(self, symbols, *, premarket, timeout):
        self.calls.append(list(symbols))
        return [quote(s, price=self.price, premarket=premarket) for s in symbols]


def run(tmp_path, symbols=('ORCL',), provider=None, now=NOW, cfg=None, snapshot=None, loader=None):
    q = PipelineQueue(tmp_path / 'queue.db')
    p = PriceStore(tmp_path / 'prices.db')
    result = discover(q, p, snapshot or identities(symbols), provider or Prices(), cfg or config(),
        clock=lambda: now, baselines_loader=loader or (lambda *args: ({s: BASE for s in symbols}, {'id': 'contract'})))
    return q, p, result


def test_price_first_without_news_persists_and_does_not_approve_contract(tmp_path):
    q, p, result = run(tmp_path)
    assert result['http_requests'] == 1 and not result['market_complete']
    assert result['discord_messages'] == result['llm_requests'] == 0
    item = price_status(p.path, 'ORCL', NOW)['observation']
    assert item['status'] == 'PRICE_WATCH' and item['indicative_gap_pct'] == pytest.approx(8)
    assert not item['eligible_for_rating'] and not item['adjustment_verified'] and not item['price_contract_verified']
    assert item['volume_status'] == item['catalyst_status'] == 'UNKNOWN'
    assert len(q.ready('NEWS', NOW)) == 1 and len(q.ready('ANALYSIS', NOW)) == 0
    assert 50 < source_priority(q, 'ORCL', NOW) < 100
    assert source_priority(q, 'ORCL', NOW + timedelta(minutes=3)) == 0
    report = latency.report(q, 'ORCL', SESSION, NOW)
    assert report['price_first_seen_at'] == timestamp(NOW) and report['events'] == []
    assert price_status(p.path, 'ORCL', NOW - timedelta(seconds=1))['observation'] is None


@pytest.mark.parametrize('raw,baseline,reason', [
    (None, BASE, 'NO_PRICE_RECORD'),
    (quote(now=NOW - timedelta(seconds=121)), BASE, 'STALE_FUTURE_OR_OUT_OF_SESSION_PRICE'),
    (quote(now=NOW + timedelta(seconds=1)), BASE, 'STALE_FUTURE_OR_OUT_OF_SESSION_PRICE'),
    (quote(premarket=False), BASE, 'PRICE_TIMESTAMP_UNIT_INVALID'),
    (quote(), None, 'PREVIOUS_SESSION_BASELINE_UNAVAILABLE'),
    (quote(), {**BASE, 'session': '2026-09-09'}, 'PREVIOUS_SESSION_BASELINE_UNAVAILABLE'),
    (quote(price=float('nan')), BASE, 'INVALID_PRICE_OR_IDENTITY'),
    (quote(symbol='CPRT'), BASE, 'INVALID_PRICE_OR_IDENTITY'),
])
def test_rejects_unknown_and_inconsistent_inputs(raw, baseline, reason):
    result = inspect_price('ORCL', raw, baseline, NOW, premarket=True, gap_threshold=4)
    assert result['status'] == 'BLOCKED' and result['reason'] == reason


def test_regular_session_uses_seconds_not_aftermarket_milliseconds():
    now = NOW.replace(hour=14)
    result = inspect_price('ORCL', quote(now=now, premarket=False), BASE, now, premarket=False, gap_threshold=4)
    assert result['status'] == 'PRICE_WATCH'
    assert inspect_price('ORCL', quote(now=now), BASE, now, premarket=False,
                         gap_threshold=4)['reason'] == 'PRICE_TIMESTAMP_UNIT_INVALID'


def test_cursor_restart_fairly_covers_more_than_one_batch(tmp_path):
    symbols = [f'S{i:03}' for i in range(230)]
    provider = Prices()
    for i, expected in enumerate([100, 100, 30]):
        q, _, result = run(tmp_path, symbols, provider)
        assert result['attempted_symbols'] == expected
        assert result['offset_start'] == i * 100
    assert sum(provider.calls, []) == symbols
    assert q.checkpoint('price:cursor')['offset'] == 0
    assert len(q.ready('NEWS', NOW, limit=5000)) == 230
    assert not result['market_complete']


def test_excludes_etf_inactive_stale_and_unsupported_identity(tmp_path):
    snapshot = identities(['ORCL', 'SOXL', 'DEAD', 'BAD.X'])
    snapshot.profiles['SOXL']['asset_type'] = 'ETF'
    snapshot.profiles['DEAD']['is_actively_trading'] = False
    provider = Prices()
    _, _, result = run(tmp_path, snapshot=snapshot, provider=provider)
    assert provider.calls == [['ORCL']] and result['unsupported_provider_symbols'] == ['BAD.X']
    snapshot.observed_at = NOW - timedelta(days=2)
    _, _, result = run(tmp_path, snapshot=snapshot, provider=provider)
    assert result['status'] == 'CURRENT_SECURITY_IDENTITIES_UNAVAILABLE'


def test_failure_retains_cursor_does_not_leak_exception_and_recovers(tmp_path):
    class Failure:
        def prices(self, *args, **kwargs):
            error = RuntimeError('secret-key-must-never-appear')
            error.response = SimpleNamespace(status_code=429)
            raise error
    q, _, result = run(tmp_path, provider=Failure())
    assert result['http_status'] == 429 and result['http_requests'] == 1
    assert result['attempted_symbols'] == 0 and 'secret' not in json.dumps(result)
    assert not q.ready('NEWS', NOW)
    _, _, recovered = run(tmp_path)
    assert recovered['attempted_symbols'] == 1


def test_missing_baseline_blocks_without_fetch_and_weekend_skips(tmp_path):
    def missing(*args):
        raise ValueError('sensitive path')
    _, _, result = run(tmp_path, loader=missing)
    assert result['status'] == 'BASELINE_UNAVAILABLE' and result['http_requests'] == 0
    _, _, result = run(tmp_path, now=NOW + timedelta(days=1))
    assert result['status'] == 'OUTSIDE_DISCOVERY_WINDOW' and result['http_requests'] == 0


def test_duplicate_quotes_do_not_create_candidate_and_fade_clears_priority(tmp_path):
    class Duplicate:
        def prices(self, *args, **kwargs):
            return [quote(), quote()]
    q, p, _ = run(tmp_path, provider=Duplicate())
    assert price_status(p.path, 'ORCL', NOW)['observation']['reason'] == 'AMBIGUOUS_PRICE_RECORDS'
    assert not q.ready('NEWS', NOW)
    provider = Prices()
    run(tmp_path, provider=provider)
    provider.price = 101
    q, _, _ = run(tmp_path, provider=provider, now=NOW + timedelta(seconds=5))
    assert source_priority(q, 'ORCL', NOW + timedelta(seconds=5)) == 0
    assert len(q.ready('NEWS', NOW + timedelta(seconds=5))) == 1
    assert latency.report(q, 'ORCL', SESSION, NOW + timedelta(seconds=5))['price_first_seen_at'] == timestamp(NOW)


def test_price_news_retries_fairly_and_retains_first_receipt(tmp_path):
    q, _, _ = run(tmp_path, ['ORCL', 'CPRT', 'RH'])
    store = EpStore(tmp_path / 'evidence.db')
    class News:
        calls = []
        rows = []
        def symbol_news(self, symbol, feed, *args, **kwargs):
            self.calls.append((symbol, feed))
            return [r for r in self.rows if r['symbol'] == symbol]
    provider = News()
    collect_price_news(q, store, provider, config(price_news_jobs=1), clock=lambda: NOW)
    first_symbol = provider.calls[0][0]
    collect_price_news(q, store, provider, config(price_news_jobs=1), clock=lambda: NOW + timedelta(minutes=10))
    assert provider.calls[2][0] != first_symbol
    provider.rows = [{'symbol': 'ORCL', 'title': 'Oracle announces first quarter fiscal 2027 results',
        'text': 'Oracle today announced financial results for its first quarter fiscal 2027.',
        'url': 'https://example.test/orcl', 'publishedDate': '2026-09-10 16:05:00'}]
    at = NOW + timedelta(minutes=20)
    result = collect_price_news(q, store, provider, config(price_news_jobs=3), clock=lambda: at)
    assert result['llm_requests'] == result['discord_messages'] == 0
    assert len(q.ready('IDENTITY', at)) == 1
    collect_price_news(q, store, provider, config(price_news_jobs=3), clock=lambda: at + timedelta(minutes=10))
    with store.connection() as db:
        assert db.execute('SELECT first_seen_at FROM ep_documents').fetchone()[0] == timestamp(at)
        assert db.execute('SELECT COUNT(*) FROM ep_runs WHERE finished_at IS NULL').fetchone()[0] == 0
    assert len(q.ready('IDENTITY', at + timedelta(minutes=10))) == 1
    timing = latency.report(q, first_symbol, SESSION, at)
    assert timing['news_search_started_at'] == timestamp(NOW)
    assert timing['price_to_news_search_seconds'] == 0


def test_news_priority_reserves_oldest_due_and_selects_large_fresh_mover(tmp_path):
    q, _, _ = run(tmp_path, ['OLD', 'SMALL', 'BIG'])
    with q.connection() as db:
        db.execute("UPDATE jobs SET due_at=? WHERE ticker='OLD'", (timestamp(NOW - timedelta(seconds=1)),))
    row = q.checkpoint('price:candidate:BIG')
    row['indicative_gap_pct'] = 44
    q.save_checkpoint('price:candidate:BIG', row, NOW)
    provider = SimpleNamespace(symbol_news=lambda *args, **kwargs: [])
    result = collect_price_news(q, EpStore(tmp_path / 'news.db'), provider, config(), clock=lambda: NOW)
    assert result['selected_tickers'] == ['OLD', 'BIG']
    assert result['capacity_deferred'] == 1 and result['attempted_jobs'] == 2


def test_news_future_and_wrong_symbol_do_not_seed_evidence(tmp_path):
    q, _, _ = run(tmp_path)
    article = {'symbol': 'ORCL', 'title': 'Oracle reports Q1 results', 'text': '',
               'url': 'https://example.test/news', 'publishedDate': '2026-09-11 08:16:00'}
    provider = SimpleNamespace(symbol_news=lambda *args, **kwargs: [article, {**article, 'symbol': 'RH'}])
    store = EpStore(tmp_path / 'news.db')
    collect_price_news(q, store, provider, config(), clock=lambda: NOW)
    assert not q.ready('IDENTITY', NOW)
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM ep_documents').fetchone()[0] == 0


def test_latency_joins_exact_analysis_and_does_not_read_future_completion(tmp_path):
    q = PipelineQueue(tmp_path / 'q.db')
    latency.record(q, 'ORCL', SESSION, 'PRICE_DISCOVERED', SESSION, NOW, {'price_at': NOW.isoformat()})
    for i in [1, 2]:
        at = NOW + timedelta(seconds=10 * i)
        job_id = q.enqueue('ANALYSIS', 'ORCL', f'source{i}', 'v1', {}, at, NOW + timedelta(hours=8))
        latency.record(q, 'ORCL', SESSION, 'SOURCE_MATCHED', str(i), at,
                       {'analysis_job_id': job_id, 'news_first_seen_at': NOW.isoformat()})
        if i == 1:
            job = q.claim('ANALYSIS', at + timedelta(seconds=5), job_id=job_id)
            q.finish(job, 'COMPLETE', 'DONE', at + timedelta(seconds=15))
    before = latency.report(q, 'ORCL', SESSION, NOW + timedelta(seconds=21))
    assert before['events'][0]['analysis_state'] == 'RUNNING'
    assert before['events'][0]['analysis_completed_at'] is None
    after = latency.report(q, 'ORCL', SESSION, NOW + timedelta(seconds=30))
    assert after['events'][0]['analysis_queue_wait_seconds'] == 5
    assert after['events'][0]['analysis_elapsed_seconds'] == 10
    assert after['events'][0]['price_to_analysis_seconds'] == 25
    assert after['events'][1]['analysis_completed_at'] is None
    with q.connection() as db:
        db.execute('DROP TABLE latency_events')
    assert latency.report(PipelineQueue(q.path, read_only=True), 'ORCL', SESSION, NOW)['events'] == []


def test_adapter_is_bounded_and_uses_symbol_specific_endpoints(monkeypatch):
    from src.data import fmp
    calls = []
    monkeypatch.setattr(fmp, '_ep_records', lambda path, params, **kwargs: calls.append((path, params)) or [])
    fmp.get_ep_price_batch(['ORCL', 'RH'], premarket=True)
    fmp.get_ep_price_batch(['ORCL'], premarket=False)
    fmp.get_ep_symbol_news('CPRT', 'stock', '2026-09-10', SESSION)
    fmp.get_ep_symbol_news('CPRT', 'press', '2026-09-10', SESSION)
    assert [c[0] for c in calls] == ['/batch-aftermarket-trade', '/batch-quote', '/news/stock', '/news/press-releases']
    assert calls[2][1] == {'symbols': 'CPRT', 'from': '2026-09-10', 'to': SESSION, 'page': 0, 'limit': 25}
    for action in (lambda: fmp.get_ep_price_batch(['A'] * 101, premarket=False),
                   lambda: fmp.get_ep_symbol_news('A?apikey=bad', 'stock', SESSION, SESSION)):
        with pytest.raises(ValueError):
            action()
    assert len(calls) == 4


def test_price_baseline_uses_execution_close_and_frozen_contract(monkeypatch):
    import pandas as pd
    from src.data import access
    from src.breakouts.ep.price_discovery import load_baselines
    contract = SimpleNamespace(target_session='2026-09-10', to_dict=lambda: {'dataset_version_id': 'v'})
    bundle = SimpleNamespace(version=SimpleNamespace(created_at=NOW - timedelta(hours=1)), contract=contract,
        bars=pd.DataFrame([{'date': '2026-09-10', 'ticker': 'ORCL', 'close': 100, 'adj_close': 90}]))
    calls = []
    monkeypatch.setattr(access, 'load_published_daily_data', lambda **kw: calls.append(kw) or bundle)
    rows, _ = load_baselines(SESSION, NOW)
    assert rows['ORCL']['close'] == 100 and calls[0]['start'] == calls[0]['end'] == '2026-09-10'
    assert calls[0]['requested_universe'] == 'US_EQUITY_COVERAGE'
    bundle.version.created_at = NOW + timedelta(seconds=1)
    with pytest.raises(ValueError, match='STALE_OR_FUTURE'):
        load_baselines(SESSION, NOW)


def test_real_sg_regular_session_receipts_replay_without_network():
    fixture = json.loads((Path(__file__).parent / 'fixtures/ep_price_discovery_20260912.json').read_text())
    assert len(fixture['records']) == 9
    for row in fixture['records']:
        result = inspect_price(row['ticker'], {'symbol': row['ticker'], **row['raw']}, row['baseline'],
            datetime.fromisoformat(row['observed_at']), premarket=False, gap_threshold=4)
        assert result['status'] == row['status']
        assert result['indicative_gap_pct'] == pytest.approx(row['indicative_gap_pct'])
        assert result['market_phase'] == 'REGULAR' and not result['eligible_for_rating']
