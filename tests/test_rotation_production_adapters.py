"""Regressions for actual SG contracts, not only hand-enriched display frames."""
from types import SimpleNamespace
from dataclasses import replace
import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from src.premarket_digest.rotation_momentum_data import BatchFrames, PartitionQuery, load_rotation_momentum_dataset
from src.group_analytics.rotation.holdings import normalize_observation, HoldingsValidationError
from src.group_analytics.rotation.reference import classified_members
from src.group_analytics.rotation.capitalization import build_observation, load_observation, heatmap_members
from src.group_analytics.rotation.heatmap import build_coverage_heatmap
from src.group_analytics.rotation.store import encoded


def test_batch_cache_is_bounded_and_does_not_truncate():
    loaded = []
    symbols = [f'S{i}' for i in range(127)]
    def load(tickers):
        loaded.extend(tickers)
        return {t: pd.DataFrame({'close': [1.]}) for t in tickers}
    frames = BatchFrames(symbols, load, 17)
    for t in symbols:
        assert frames[t].close.iloc[0] == 1
        assert len(frames.cache) <= 17
    assert loaded == symbols
    assert frames.get('UNKNOWN') is None
    frames['S0']
    assert len(frames.cache) == 17


def test_failed_batch_is_not_cached_as_success():
    calls = []
    def fail(tickers):
        calls.append(tickers)
        raise ValueError('bad partition')
    frames = BatchFrames(['A'], fail)
    for _ in range(2):
        with pytest.raises(ValueError):
            frames['A']
    assert len(calls) == 2 and frames.cached_batch is None


def test_real_parquet_batches_preserve_report_and_contract(tmp_path, monkeypatch):
    import src.breakouts.broad_daily_data as broad
    from src.breakouts.daily_data import daily_frames_from_bars
    from src.premarket_digest.momentum import CompletedSessionMomentumSource
    from src.premarket_digest.settings import PremarketDigestSettings
    from src.alerts.config import AlertSettings
    dates = pd.bdate_range('2026-01-01', periods=100)
    symbols = ['AAA', 'BBB', 'CCC', 'QQQ']
    records = []
    for n, ticker in enumerate(symbols):
        for i, date in enumerate(dates):
            close = 100 * (1.003 + n * .001) ** i
            records.append(dict(date=date, ticker=ticker, security_id='id'+ticker,
                                open=close, high=close*1.04, low=close*.96, close=close,
                                adj_close=close, volume=1e7))
    bars = pd.DataFrame(records)
    path = tmp_path / 'bars.parquet'
    bars.to_parquet(path)
    metadata = pd.DataFrame([dict(ticker=t, security_id='id'+t, name=t, sector='Technology',
                                asset_type='ETF' if t == 'QQQ' else 'STOCK',
                                current_dollar_volume=1e9, is_current_coverage=True) for t in symbols])
    parent = SimpleNamespace(version_id='pinned', target_session=dates[-1].date(), created_at=pd.Timestamp('2026-06-01T00:00:00Z'))
    contracts = []
    def contract(**kwargs):
        cov = kwargs['coverage'].to_dict()
        contracts.append(cov)
        return SimpleNamespace(target_session=str(parent.target_session), dataset_version_id='pinned', to_dict=lambda: cov)
    monkeypatch.setattr(broad, '_resolve_context', lambda **kw: (parent, {}, None, metadata.iloc[:3], metadata, dates[-1]))
    monkeypatch.setattr(broad, '_contract', contract)
    checked = []
    reader = SimpleNamespace(verify_version=lambda v, **kw: checked.append(v.version_id), partition_paths=lambda *a, **kw: [path])
    lazy = load_rotation_momentum_dataset(requested_universe='US_ACTIVE', ticker_selector=lambda m: [*m.ticker, 'QQQ'],
                                         end=str(parent.target_session), min_latest_coverage=1., reader=reader, batch_size=2)
    eager = replace(lazy, frames=daily_frames_from_bars(bars))
    settings = PremarketDigestSettings(momentum_min_exact_asof_coverage=1., momentum_min_evaluable_coverage=1.)
    def report(dataset):
        return CompletedSessionMomentumSource(settings, alert_settings=AlertSettings(),
                  dataset_loader=lambda **kw: dataset).load(str(parent.target_session))
    assert encoded(report(lazy)) == encoded(report(eager))
    assert checked == ['pinned']
    assert contracts[0]['latest_coverage'] == 1
    assert set(contracts[0]['requested_tickers']) == set(symbols)
    assert lazy.frames.batch_size == 2
    # Missing benchmark prices must affect the whole-input coverage gate.
    bars.loc[bars.ticker.eq('QQQ') & bars.date.eq(dates[-1]), 'date'] = dates[-2]
    bars.to_parquet(path)
    from src.data.access import MarketDataNotReadyError
    with pytest.raises(MarketDataNotReadyError) as exc:
        load_rotation_momentum_dataset(requested_universe='US_ACTIVE', ticker_selector=lambda m: [*m.ticker, 'QQQ'],
                                       end=str(parent.target_session), min_latest_coverage=1., reader=reader, batch_size=2)
    assert exc.value.coverage.latest_coverage == .75


def equity_row(asset='AAA', weight=99.):
    return {'symbol': 'SMH', 'asset': asset, 'isin': 'US'+asset, 'weightPercentage': weight}


def test_multiple_cash_and_signed_derivative_rows_reconcile():
    rows = [equity_row(weight=99.9),
            {'symbol':'SMH', 'asset':'', 'name':'-USD CASH-', 'weightPercentage':.08},
            {'symbol':'SMH', 'asset':'', 'name':'Other/Cash', 'weightPercentage':.04},
            {'symbol':'SMH', 'asset':'IXTU6', 'name':'XAK TECHNOLOGY    SEP26', 'securityCusip':'ADI394XJ2', 'weightPercentage':-.02}]
    obs = normalize_observation(rows, 'SMH', '2026-09-13T00:00:00Z',
                                securities={'AAA': {'security_id':'equity1', 'isin':'USAAA'}}, reference_id='master1')
    assert obs['reported_weight_pct'] == pytest.approx(100.)
    assert obs['negative_excluded_weight_pct'] == -.02
    assert len(obs['members']) == 1 and len(obs['excluded']) == 3
    assert obs['members'][0]['security_id'] == 'equity1'
    assert obs['identity_policy'] == 'BOUND_SECURITY_MASTER'


def test_unknown_empty_assets_are_not_assumed_cash():
    obs = normalize_observation([equity_row(), {'symbol':'SMH','asset':'','name':'RAINBOW ROBOTICS','weightPercentage':1}],
                                'SMH', '2026-09-13T00:00:00Z')
    assert obs['excluded'][0]['reason'] == 'UNRESOLVED_LISTING'
    assert obs['excluded'][0]['name'] == 'RAINBOW ROBOTICS'


def test_futures_like_ticker_is_not_resolved_by_regex_in_production():
    obs = normalize_observation([equity_row(), {'symbol':'SMH','asset':'FUTU6','isin':'SOMETHING','weightPercentage':1}],
                                'SMH', '2026-09-13T00:00:00Z', securities={'AAA':{'security_id':'a','isin':'USAAA'}})
    assert [m['ticker'] for m in obs['members']] == ['AAA']
    assert obs['excluded'][0]['reason'] == 'UNRESOLVED_US_EQUITY'


@pytest.mark.parametrize('bad', ['negative', 'duplicate', 'infinite', 'conflict'])
def test_equity_integrity_still_blocks_or_excludes(bad):
    rows = [equity_row(weight=50.), equity_row('BBB', 50.)]
    securities = {'AAA':{'security_id':'a','isin':'USAAA'}, 'BBB':{'security_id':'b','isin':'USBBB'}}
    if bad == 'negative': rows[0]['weightPercentage'] = -1
    if bad == 'duplicate': rows[1] = rows[0].copy()
    if bad == 'infinite': rows[0]['weightPercentage'] = float('inf')
    if bad == 'conflict':
        securities['AAA']['isin'] = 'DIFFERENT'
        obs = normalize_observation(rows,'SMH','2026-09-13T00:00:00Z',securities=securities)
        assert len(obs['members']) == 1
        assert obs['excluded'][0]['reason'] == 'IDENTITY_UNRESOLVED_OR_CONFLICT'
    else:
        with pytest.raises(ValueError): normalize_observation(rows,'SMH','2026-09-13T00:00:00Z',securities=securities)


def raw_members():
    return pd.DataFrame({'security_id':['a','b','q'], 'ticker':['AAA','DEAD','QQQ'],
                         'primary_exchange':['NASDAQ']*3, 'is_current_coverage':[True,False,True],
                         'coverage_role':['EQUITY','EQUITY','BENCHMARK_ONLY']})


def test_real_coverage_schema_classification_and_current_filter():
    classes = pd.DataFrame({'security_id':['a','b','q'], 'sector':['Technology','Energy','Benchmark'],
                            'sub_industry':['Software','Oil','Index'], 'effective_from':['2020-01-01']*3,
                            'effective_to':[None]*3, 'knowledge_date':['2026-09-11']*3})
    joined = classified_members(raw_members(), {'classifications':classes}, '2026-09-11')
    assert joined.ticker.tolist() == ['AAA','QQQ']
    assert joined.sector.tolist() == ['Technology','Benchmark']


def write_cap(root, data):
    digest = hashlib.sha256(encoded(data)).hexdigest()
    (root/'observations').mkdir(exist_ok=True)
    (root/'observations'/f'{digest}.json').write_bytes(encoded(data))
    (root/'latest.json').write_text(json.dumps({'sha256':digest}))


def test_cap_snapshot_has_time_identity_coverage_and_rejects_stale(tmp_path):
    provider = pd.DataFrame({'ticker':['AAA','DEAD'],'market_cap':[123.,456.],'exchange':['NASDAQ']*2})
    obs = build_observation(raw_members(),provider,version_id='v',generation_id='g',source_session='2026-09-11',captured_at='2026-09-13T00:00:00Z')
    assert obs['eligible'] == 1 and obs['mapped'] == 1
    assert obs['members'] == [{'security_id':'a','ticker':'AAA','market_cap':123.}]
    write_cap(tmp_path, obs)
    version = SimpleNamespace(version_id='v',target_session='2026-09-11')
    assert load_observation(version,root=tmp_path,now='2026-09-13T01:00:00Z')['coverage'] == 1.
    for now in ['2026-09-12T23:00:00Z','2026-09-17T00:00:00Z']:
        with pytest.raises(ValueError): load_observation(version,root=tmp_path,now=now)
    with pytest.raises(ValueError): load_observation(SimpleNamespace(version_id='other',target_session='2026-09-11'),root=tmp_path,now='2026-09-13T01:00:00Z')


def test_empty_caps_never_produce_available_heatmap():
    prices = pd.DataFrame({'ticker':['AAA','QQQ'],'date':['2026-09-11']*2,'adj_close':[100.,100.]})
    result = build_coverage_heatmap(raw_members(),prices,source_session='2026-09-11')
    assert result['status'] == 'unavailable' and result['reason'] == 'NO_USABLE_MARKET_CAP'
    assert result['counts']['coverage_current'] == 2
    assert result['counts']['eligible'] == 1


def test_partial_acquisition_is_not_success():
    from scripts.observe_rotation_holdings import summarize_observations
    results = [{'status':'SUCCESS'}] + [{'status':'FAILED'} for _ in range(16)]
    summary = summarize_observations(results, '/isolated')
    assert summary['status'] == 'PARTIAL'
    assert summary['failed'] == 16 and summary['succeeded'] == 1
    assert summarize_observations([], '/isolated')['status'] == 'FAILED'


def test_holdings_identity_resolution_changes_publication_fingerprint():
    from src.group_analytics.rotation.holdings import holdings_fingerprint
    import copy
    old = normalize_observation([equity_row(weight=100)],'SMH','2026-09-13T00:00:00Z')
    changed = copy.deepcopy(old)
    changed['members'][0]['security_id'] = 'new-security'
    assert old['response_sha256'] == changed['response_sha256']
    assert holdings_fingerprint({'SMH':old}) != holdings_fingerprint({'SMH':changed})


def test_cap_publication_does_not_overwrite_newer_or_changed_parent(tmp_path, monkeypatch):
    import src.group_analytics.rotation.capitalization as cap
    import src.group_analytics.calendar as calendar
    version = SimpleNamespace(version_id='v',target_session='2026-09-11')
    class Reader:
        latest = version
        def load_universe(self, *a, **kw): return raw_members()
        def verify_version(self, *a, **kw): return {}
        def require_latest(self, *a, **kw): return self.latest
    reader = Reader()
    monkeypatch.setattr(cap,'bound_reference',lambda *a: (version, {}, SimpleNamespace(generation_id='g')))
    monkeypatch.setattr(calendar,'latest_completed_session',lambda **kw: pd.Timestamp('2026-09-11'))
    provider = lambda: pd.DataFrame({'ticker':['AAA'],'market_cap':[123.],'exchange':['NASDAQ']})
    kwargs = dict(reader=reader,provider_loader=provider,root=tmp_path)
    cap.refresh_observation(**kwargs,now='2026-09-13T01:00:00Z')
    before = (tmp_path/'latest.json').read_bytes()
    with pytest.raises(ValueError,match='Newer'):
        cap.refresh_observation(**kwargs,now='2026-09-13T00:00:00Z')
    assert (tmp_path/'latest.json').read_bytes() == before
    reader.latest = SimpleNamespace(version_id='new')
    with pytest.raises(ValueError,match='moved'):
        cap.refresh_observation(**kwargs,now='2026-09-13T02:00:00Z')
    assert (tmp_path/'latest.json').read_bytes() == before
