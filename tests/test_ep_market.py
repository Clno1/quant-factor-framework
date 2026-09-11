from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import sys

import pytest

from src.breakouts.ep.market import MarketContract, Minute, SessionTape, evaluate_market, rank_candidate
from src.breakouts.ep.market_worker import MarketShadowStore, collect_market, replay_market
from src.breakouts.ep.gap_detector import confirm_gap
from src.breakouts.ep.market_session import previous_xnys_sessions, xnys_session_schedule
from src.breakouts.ep.models import NEW_YORK
from test_ep_radar import ROOT


def bar(at, price=110., volume=3000, **changes):
    row = {'starts_at': at.isoformat(), 'open': price, 'high': price + 1, 'low': price - 1,
           'close': price, 'volume': volume, 'dollar_volume': price * volume}
    row.update(changes)
    return row


def tape(date, factor=1):
    opens = xnys_session_schedule(date).opens_at
    bars = [bar(opens - timedelta(minutes=1), volume=100000 * factor)]
    bars += [bar(opens + timedelta(minutes=i), volume=1000 * factor) for i in range(6)]
    if factor == 3:
        bars[-1] = bar(opens + timedelta(minutes=5), price=112., volume=3000)
    return {'ticker': 'TEST', 'session': date, 'contract_revision': 'fixture-v1',
            'covered_from': opens.replace(hour=4, minute=0).isoformat(), 'covered_until': (opens + timedelta(hours=1)).isoformat(),
            'coverage_verified': True, 'bars': bars}


def bundle():
    now = datetime(2026, 9, 11, 9, 36, tzinfo=NEW_YORK)
    history = previous_xnys_sessions('2026-09-11', 20)
    return {'as_of': now.isoformat(), 'contract': {'revision': 'fixture-v1', 'evidence_ids': ['TEST_CONTRACT_ONLY'],
                'timezone_verified': True, 'minute_volume_verified': True, 'extended_hours_verified': True,
                'dollar_turnover_verified': True, 'adjustment_verified': True},
            'current': tape('2026-09-11', 3), 'history': [tape(d) for d in history],
            'previous_close': 100., 'previous_session': history[-1],
            'catalyst': {'ticker': 'TEST', 'session': '2026-09-11', 'evidence_ids': ['TEST_CATALYST_ONLY'],
                'direct': True, 'fresh': True, 'source_verified': True, 'quality': 'STRONG'},
            'cup': {'ticker': 'TEST', 'source_session': history[-1], 'snapshot_id': 'fixture-cup',
                'eligible': True, 'rim': 105., 'previous_close': 100.}}


def test_gap_rvol_vwap_and_orh_confirm_only_in_shadow():
    result = replay_market(bundle())
    m = result['metrics']
    assert not m['blockers']
    assert m['premarket_rvol'] == 3 and m['regular_rvol'] == 3
    assert m['gap_pct'] == pytest.approx(10)
    assert m['premarket_dollar_volume'] == 33000000
    assert result['ranking']['grade'] == 'STRONG'
    trigger = result['confirmation']
    assert trigger['status'] == 'SHADOW_CONFIRMED' and trigger['family'] == 'EP_GAP_THROUGH_CUP'
    assert trigger['orh'] == 111 and trigger['stop_reference'] == 109
    assert result['external_requests'] == 0 and result['delivery'] == 'DISABLED_SHADOW_ONLY'


@pytest.mark.parametrize('mutate,reason', [
    (lambda b: b['contract'].update(minute_volume_verified=False), 'MINUTE_VOLUME_VERIFIED_REQUIRED'),
    (lambda b: b['history'].pop(), 'TWENTY_SAME_CLOCK_HISTORY_SESSIONS_REQUIRED'),
    (lambda b: b['current'].update(coverage_verified=False), 'WINDOW_COVERAGE_NOT_VERIFIED'),
    (lambda b: b['current']['bars'][0].update(dollar_volume=None), 'TRUE_DOLLAR_TURNOVER_REQUIRED'),
    (lambda b: b.update(previous_session='2026-09-09'), 'PREVIOUS_CLOSE_OR_CURRENT_SESSION_MISMATCH'),
    (lambda b: b['history'].append(deepcopy(b['history'][0])), 'DUPLICATE_HISTORY_SESSIONS'),
])
def test_market_contract_and_missing_data_stop_confirmation(mutate, reason):
    b = bundle()
    mutate(b)
    result = replay_market(b)
    assert reason in result['metrics']['blockers']
    assert result['confirmation']['status'] != 'SHADOW_CONFIRMED'


def test_no_cup_remains_ep_watch_and_theme_cannot_confirm():
    b = bundle()
    b.pop('cup')
    assert replay_market(b)['confirmation']['family'] == 'EP_WATCH'
    b = bundle()
    b['catalyst']['direct'] = False
    assert replay_market(b)['ranking']['grade'] == 'HEADS_UP'
    b = bundle()
    b['cup']['previous_close'] = 108.
    assert 'CUP_REFERENCE_PRICE_MISMATCH' in replay_market(b)['confirmation']['reasons']
    b = bundle()
    b['cup']['rim'] = 115.
    assert 'NOT_AN_OVERNIGHT_GAP_THROUGH_CUP' in replay_market(b)['confirmation']['reasons']


def test_forming_bar_not_a_trigger_and_only_crossing_once():
    b = bundle()
    b['as_of'] = '2026-09-11T09:35:59-04:00'
    assert 'WAIT_FOR_BAR_AFTER_OPENING_RANGE' in replay_market(b)['confirmation']['reasons']
    b['as_of'] = '2026-09-11T09:37:00-04:00'
    b['current']['bars'].append(bar(datetime(2026, 9, 11, 9, 36, tzinfo=NEW_YORK), price=113.))
    for old in b['history']:
        opens = xnys_session_schedule(old['session']).opens_at
        old['bars'].append(bar(opens + timedelta(minutes=6), volume=1000))
    assert 'NO_NEW_CLOSED_BAR_ORH_CROSS' in replay_market(b)['confirmation']['reasons']


def test_same_clock_baseline_does_not_include_later_volume():
    b = bundle()
    for old in b['history']:
        opens = xnys_session_schedule(old['session']).opens_at
        old['bars'].append(bar(opens + timedelta(minutes=10), volume=999999999))
    assert replay_market(b)['metrics']['regular_rvol'] == 3


def test_timestamps_duplicates_and_ohlcv_constraints():
    row = bundle()['current']['bars'][0]
    with pytest.raises(ValueError, match='MINUTE_START'):
        Minute.model_validate({**row, 'starts_at': '2026-09-11T09:29:00'})
    with pytest.raises(ValueError, match='INVALID_OHLC'):
        Minute.model_validate({**row, 'low': 900.})
    b = bundle()['current']
    b['bars'].append(b['bars'][0])
    with pytest.raises(ValueError, match='DUPLICATE_MINUTES'):
        SessionTape.model_validate(b)
    assert xnys_session_schedule('2026-11-27').expected_minutes == 210
    assert xnys_session_schedule('2026-12-01').opens_at.astimezone(timezone.utc).hour == 14


def test_raw_capture_has_no_volume_assumptions_and_signal_dedup(tmp_path):
    class FakeProvider:
        def get_ep_extended_batch(self, symbols, **kw):
            return [{'symbol': 'TEST', 'price': 110., 'size': 5, 'api_key': 'never-store-this'}]
        def get_ep_minute_day(self, *a, **kw):
            return [{'date': '2026-09-11 09:29:00', 'volume': 1000}]
    store = MarketShadowStore(tmp_path / 'market.sqlite3')
    result = collect_market(store, ['TEST'], provider=FakeProvider())
    assert result['external_requests'] == 3
    with store.connection() as db:
        rows = db.execute('SELECT payload_json FROM ep_market_receipts').fetchall()
    assert len(rows) == 3 and 'never-store-this' not in str(rows)
    assert 'MARKET_DATA_CONTRACT_NOT_VERIFIED' in result['confirmation_blockers']
    report = replay_market(bundle())
    for i in range(2):
        store.save('TEST', 'SHADOW_REPORT', report, datetime.now(timezone.utc))
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM ep_market_signals').fetchone()[0] == 1


def test_market_cli_plan_and_replay_are_network_free(tmp_path):
    db = tmp_path / 'market.sqlite3'
    cmd = [sys.executable, str(ROOT / 'scripts/run_ep_market_shadow.py'), '--database', str(db)]
    result = subprocess.run(cmd + ['--symbols', 'TEST'], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['external_requests'] == 0 and not db.exists()
    fixture = tmp_path / 'replay.json'
    fixture.write_text(json.dumps(bundle()))
    result = subprocess.run(cmd + ['--input', str(fixture)], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)['confirmation']['status'] == 'SHADOW_CONFIRMED'
    assert not db.exists()


def test_fmp_adapter_uses_stable_endpoints_and_bounded_single_day(monkeypatch):
    from src.data import fmp
    calls = []
    def records(path, params, **kw):
        calls.append((path, params, kw))
        return []
    monkeypatch.setattr(fmp, '_ep_records', records)
    fmp.get_ep_extended_batch(['GTLB', 'PLAB'], kind='trade')
    fmp.get_ep_extended_batch(['GTLB'], kind='quote')
    fmp.get_ep_minute_day('GTLB', '2026-09-11')
    assert calls[0][0] == '/batch-aftermarket-trade'
    assert calls[1][0] == '/batch-aftermarket-quote'
    assert calls[2][0] == '/historical-chart/1min'
    assert calls[2][1]['from'] == calls[2][1]['to'] == '2026-09-11'
    with pytest.raises(ValueError):
        fmp.get_ep_extended_batch(['TEST'] * 101)
    with pytest.raises(ValueError):
        fmp.get_ep_minute_day('TEST?apikey=foo', '2026-09-11')


def test_sg_trade_size_and_quote_volume_are_preserved_but_not_approved(tmp_path):
    class Provider:
        def get_ep_extended_batch(self, symbols, *, kind, **kwargs):
            if kind == 'trade':
                return [{'symbol': 'VEEV', 'price': 260.31, 'tradeSize': 15, 'timestamp': 1789084249000}]
            return [{'symbol': 'VEEV', 'bidPrice': 260, 'askPrice': 268,
                     'volume': 853369, 'timestamp': 1789084802000}]
        def get_ep_minute_day(self, *args, **kwargs):
            return []
    store = MarketShadowStore(tmp_path / 'market.sqlite3')
    result = collect_market(store, ['VEEV'], now=datetime(2026, 9, 11, 7, tzinfo=timezone.utc), provider=Provider())
    trade, quote, minute = result['outcomes']
    assert 'EXTENDED_SNAPSHOT_NOT_CURRENT_SESSION' in trade['diagnostics']['reasons']
    assert 'STALE_EXTENDED_SNAPSHOT' in quote['diagnostics']['reasons']
    assert 'QUOTE_VOLUME_SCOPE_UNVERIFIED' in quote['diagnostics']['reasons']
    assert minute['diagnostics']['reasons'] == ['NO_RECORDS_NOT_PROOF_OF_NO_TRADING']
    with store.connection() as db:
        payload = json.loads(db.execute("SELECT payload_json FROM ep_market_receipts WHERE kind='EXTENDED_TRADE'").fetchone()[0])
    assert payload['records'][0]['tradeSize'] == 15 and not payload['eligible_for_rating']


@pytest.mark.parametrize('status', [401, 403, 429])
def test_market_access_refusal_stops_and_persists_per_symbol_receipts(tmp_path, status):
    import requests
    class Provider:
        calls = 0
        def get_ep_extended_batch(self, *args, **kwargs):
            self.calls += 1
            response = requests.Response()
            response.status_code = status
            raise requests.HTTPError('never-store-api-key', response=response)
    provider = Provider()
    store = MarketShadowStore(tmp_path / 'market.sqlite3')
    result = collect_market(store, ['GTLB', 'NYAX'], provider=provider)
    assert provider.calls == result['external_requests'] == 1
    assert len(result['outcomes']) == 6
    assert all(o['receipt_id'] and o['ticker'] for o in result['outcomes'])
    assert [o['status'] for o in result['outcomes']].count('MARKET_FETCH_FAILED') == 2
    with store.connection() as db:
        rows = db.execute('SELECT payload_json FROM ep_market_receipts').fetchall()
    assert len(rows) == 6 and 'never-store-api-key' not in str(rows)


def test_sg_missing_opening_bars_and_fractional_volume_are_diagnostic_only():
    from src.breakouts.ep.market_quality import inspect_market_records
    rows = [{'date': f'2026-09-02 09:{m}:00', 'volume': 100.25} for m in (32, 33, 34)]
    result = inspect_market_records('MINUTE_DAY', rows, session='2026-09-02',
                                    as_of=datetime(2026, 9, 11, tzinfo=timezone.utc))
    assert result['missing_opening_labels']['5'] == ['09:30', '09:31']
    assert {'OPENING_1M_LABELS_INCOMPLETE', 'OPENING_5M_LABELS_INCOMPLETE',
            'FRACTIONAL_VOLUME_OBSERVED', 'PREMARKET_MINUTE_COVERAGE_NOT_OBSERVED'} <= set(result['reasons'])
    assert not result['contract_verified'] and not result['eligible_for_rating']
    assert rows[0]['volume'] == 100.25


def test_quality_retains_closed_session_and_invalid_time_diagnostics():
    from src.breakouts.ep.market_quality import inspect_market_records
    now = datetime(2026, 9, 12, tzinfo=timezone.utc)
    result = inspect_market_records('MINUTE_DAY', [{'date': 'bad', 'volume': -1}], session='2026-09-12', as_of=now)
    assert {'NOT_AN_XNYS_SESSION', 'MINUTE_DATE_OR_SESSION_INVALID', 'MINUTE_VOLUME_INVALID'} <= set(result['reasons'])
    for epoch in (1789084249, '1789084249000', None):
        result = inspect_market_records('EXTENDED_TRADE', [{'timestamp': epoch}], session='2026-09-11', as_of=now)
        assert 'EXTENDED_TIMESTAMP_MILLISECONDS_REQUIRED' in result['reasons']


def test_future_opening_window_is_not_reported_as_missing_market_data():
    from src.breakouts.ep.market_quality import inspect_market_records
    rows = [{'date': '2026-09-11 04:00:00', 'volume': 10}]
    before = inspect_market_records('MINUTE_DAY', rows, session='2026-09-11',
                                   as_of=datetime(2026, 9, 11, 8, 5, tzinfo=timezone.utc))
    assert set(before['opening_window_states'].values()) == {'NOT_YET_CLOSED'}
    assert not any(r.startswith('OPENING_') for r in before['reasons'])
    after = inspect_market_records('MINUTE_DAY', rows, session='2026-09-11',
                                  as_of=datetime(2026, 9, 11, 13, 36, tzinfo=timezone.utc))
    assert after['opening_window_states']['15'] == 'NOT_YET_CLOSED'
    assert 'OPENING_5M_LABELS_INCOMPLETE' in after['reasons']


def test_sg_fresh_premarket_quote_does_not_certify_fractional_volume():
    from src.breakouts.ep.market_quality import inspect_market_records
    row = {'symbol': 'AAPL', 'timestamp': 1789113679000, 'volume': 49230.30225,
           'bidPrice': 325.23, 'askPrice': 326.57}
    result = inspect_market_records('EXTENDED_QUOTE', [row], session='2026-09-11',
                                    as_of=datetime(2026, 9, 11, 8, 1, 21, tzinfo=timezone.utc))
    assert 'STALE_EXTENDED_SNAPSHOT' not in result['reasons']
    assert {'QUOTE_VOLUME_SCOPE_UNVERIFIED', 'QUOTE_FIELD_TIMESTAMPS_NOT_PROVIDED',
            'FRACTIONAL_QUOTE_VOLUME_OBSERVED'} <= set(result['reasons'])
    assert not result['contract_verified'] and row['volume'] == 49230.30225
