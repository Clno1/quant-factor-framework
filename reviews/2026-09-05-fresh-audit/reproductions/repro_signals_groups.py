"""Isolated fresh audit: no real data, network, or notifications.
Run from isolated repo with its dependency-complete Python runtime.
Assertions confirm current defects; this script intentionally does not patch them.
"""
from __future__ import annotations
import argparse
from dataclasses import replace
from datetime import datetime, date
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path('/private/tmp/quant_fresh_audit_20260905/repo')
sys.path.insert(0, str(ROOT))
NY = ZoneInfo('America/New_York')

def load_rolling_without_package_init():
    # Load unmodified self-contained source to avoid eager package imports.
    spec = importlib.util.spec_from_file_location('audit_rolling', ROOT / 'src/breakouts/live/rolling.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RollingIntradayBars

def rolling():
    RollingIntradayBars = load_rolling_without_package_init()
    index = pd.to_datetime(['2026-07-28 10:00:00'])
    partial = pd.DataFrame({'open':[100.0], 'high':[105.0], 'low':[99.0], 'close':[105.0], 'volume':[100.0]}, index=index)
    final = pd.DataFrame({'open':[100.0], 'high':[106.0], 'low':[94.0], 'close':[95.0], 'volume':[1000.0]}, index=index)
    storage = RollingIntradayBars('TEST')
    storage.merge(partial)
    before = storage.completed_frame(datetime(2026,7,28,10,0,8,tzinfo=NY))
    assert before.empty
    correction_count = storage.merge(final)
    after = storage.completed_frame(datetime(2026,7,28,10,1,8,tzinfo=NY))
    metrics = storage.metrics(now=datetime(2026,7,28,10,1,8,tzinfo=NY),session_date='2026-07-28',interval=1)
    assert after.iloc[-1]['close'] == 105.0 and after.iloc[-1]['volume'] == 100.0
    assert metrics['last_price'] == 105.0
    return {'correction_merge_count':correction_count,'retained_close':float(after.iloc[-1]['close']),'expected_final_close':95.0,'retained_volume':float(after.iloc[-1]['volume']),'expected_final_volume':1000.0,'metrics_last_price':metrics['last_price']}

def outcomes():
    from src.breakouts.historical_backtest import _event_outcomes
    frame=pd.DataFrame({'open':[100.0]*3,'high':[101.0,101.0,150.0],'low':[99.0,99.0,50.0],'close':[100.0]*3,'adj_close':[100.0]*3,'volume':[1000.0]*3},index=pd.bdate_range('2026-07-27',periods=3))
    result=_event_outcomes(frame,signal_position=0,horizons=(1,),round_trip_cost_bps=0)
    assert result['h1_gross_return']==0 and result['h1_mae']==-0.5 and result['h1_mfe']==0.5
    return {**result,'held_session_only_mae':-0.01,'held_session_only_mfe':0.01}

def censored():
    from src.breakouts.historical_backtest import backtest_breakout_frames, BreakoutBacktestConfig
    frame=pd.DataFrame({'open':100.0,'high':101.0,'low':99.0,'close':100.0,'adj_close':100.0,'volume':1000.0},index=pd.bdate_range('2026-01-01',periods=81))
    def scanner(prefix, **_):
        return {'status':'BREAKOUT' if len(prefix)==len(frame) else 'FORMING','base_pass':True,'close':100.0,'pivot':100.0}
    with patch('src.breakouts.historical_backtest.evaluate_daily_setup',side_effect=scanner):
        try:
            backtest_breakout_frames({'TEST':frame}, config=BreakoutBacktestConfig(horizons=(1,)))
        except AttributeError as error:
            assert 'dropna' in str(error)
            return {'only_event_on':str(frame.index[-1].date()),'actual_error_type':type(error).__name__,'actual_error':str(error),'expected':'one censored event, zero realized observations'}
    raise AssertionError('expected censored-entry crash was not reproduced')

def watchlist():
    from src.breakouts.application import get_breakout_scan, BreakoutScanNotReadyError
    params=dict(enabled_universes=('US_ACTIVE',),asof=None,min_return_20d=20,min_adr_20d=6,min_dollar_volume_m=10,min_avg_dollar_volume_m=10,min_consolidation_days=9,max_distance_ma50=35,pivot_proximity=3,market_symbol='QQQ',view='all',allow_build=False)
    with patch('src.breakouts.application.resolve_breakout_universe',return_value={'data_universe':'US_LIQUID_5M','dataset_version_id':'fixture-v1'}),patch('src.breakouts.application.load_scan_cache',return_value={'rows':[{'ticker':'TEST'}]}) as cache:
        ordinary=get_breakout_scan(universe='US_ACTIVE',**params)
        cache.reset_mock()
        try:
            get_breakout_scan(universe='watchlist:mine',**params)
        except BreakoutScanNotReadyError as error:
            assert cache.call_count==0
            return {'ordinary_scan_succeeds':bool(ordinary['rows']),'watchlist_error':str(error),'watchlist_cache_reads':cache.call_count}
    raise AssertionError('expected unavailable watchlist was not reproduced')

def stale_quote():
    from src.alerts.engine import run_live_alert_scan
    from src.alerts.config import AlertSettings
    days=pd.bdate_range(end='2026-07-27',periods=80)
    closes=20.0*np.power(1.02,np.arange(80))
    frame=pd.DataFrame({'open':closes*.99,'high':closes*1.04,'low':closes*.96,'close':closes,'volume':2_000_000.0},index=days)
    tickers=['STALE','FRESH']
    universe=pd.DataFrame({'ticker':tickers,'name':tickers,'sector':['Technology']*2,'asset_type':['STOCK']*2,'current_dollar_volume':[50e6]*2})
    quotes=pd.DataFrame({'ticker':tickers,'price':[closes[-1]]*2,'open':[closes[-1]*.99]*2,'dayHigh':[closes[-1]*1.04]*2,'dayLow':[closes[-1]*.96]*2,'volume':[2e6]*2,'timestamp':[datetime(2026,7,27,15,59,tzinfo=NY).timestamp(),datetime(2026,7,28,10,30,tzinfo=NY).timestamp()]}).set_index('ticker')
    daily=SimpleNamespace(data_universe='US_LIQUID_5M',dataset_version_id='fixture-v1',contract=SimpleNamespace(to_dict=lambda:{}),frame=lambda _:frame,universe=universe)
    with patch('src.alerts.engine._forced_tickers',return_value=set()),patch('src.alerts.engine._broad_pool',return_value=(tickers,universe,{'asof':'2026-07-27'},set(),set(),2)),patch('src.alerts.engine.get_batch_quotes',return_value=quotes),patch('src.alerts.engine.load_market_regime',return_value={}):
        result=run_live_alert_scan(AlertSettings(),market_hours={'isMarketOpen':True},include_intraday=False,dataset_loader=lambda **_:daily)
    old=next(row for row in result['rows'] if row['ticker']=='STALE')
    assert old['data_date']=='2026-07-28' and old['quote_timestamp'].startswith('2026-07-27') and old['base_pass']
    return {'batch_session':result['session_date'],'stale_quote_time':old['quote_timestamp'],'stale_row_data_date':old['data_date'],'stale_row_strict_pass':old['base_pass'],'stale_row_signal':old['signal_type'],'strict_count':result['strict_count']}

def zero_cup_volume():
    from tests.test_cup_handle import _candidate, _handle_bars, _quote
    from src.breakouts.live.cup_handle import CupHandleDetector,CUP_HANDLE_ALGORITHM_VERSION,CUP_HANDLE_PARAMETER_VERSION
    from src.breakouts.live.settings import IntradayMonitorSettings
    from src.breakouts.live.state import IntradayMonitorState
    settings=IntradayMonitorSettings()
    candidate=_candidate(settings)
    bars=_handle_bars(candidate)
    for bar in bars[6:]:
        bar['volume']=0.0
    now=datetime(2026,4,9,10,35,1,tzinfo=NY)
    evaluation=CupHandleDetector(settings).evaluate(candidate,_quote(bars),{'bars':bars,'error':None},now=now,session_date='2026-04-09',market_open=True)
    assert evaluation.outcome=='MATCH' and np.isinf(evaluation.details['breakout_volume_ratio'])
    with tempfile.TemporaryDirectory(prefix='audit-cup-',dir=ROOT.parent) as temp:
        state=IntradayMonitorState(Path(temp)/'state.sqlite3')
        try:
            state.record_cup_handle_cycle({'session_date':'2026-04-09','observed_at':now.isoformat(),'algorithm_version':CUP_HANDLE_ALGORITHM_VERSION,'parameter_version':CUP_HANDLE_PARAMETER_VERSION},[evaluation.to_dict()])
        except ValueError as error:
            assert 'Out of range float' in str(error)
            return {'zero_handle_and_breakout_volume':True,'actual_outcome':evaluation.outcome,'breakout_volume_ratio':str(evaluation.details['breakout_volume_ratio']),'actual_persistence_error':str(error)}
    raise AssertionError('expected nonfinite cup serialization crash was not reproduced')

def mapping_ignored():
    from src.group_analytics.settings import GroupAnalyticsSettings,ClassificationSettings
    from src.group_analytics.service import GroupAnalyticsService
    from src.group_analytics.adapters import FMPCurrentClassificationProvider
    with tempfile.TemporaryDirectory(prefix='audit-mapping-',dir=ROOT.parent) as temp:
        path=Path(temp)/'custom.yaml'
        content=(ROOT/'configs/classifications/fmp_group_ids.yaml').read_text().replace('version: "2026-07-16"','version: "audit-custom-v2"')
        path.write_text(content)
        settings=GroupAnalyticsSettings(classification=ClassificationSettings(group_id_mapping_path=str(path)))
        service=GroupAnalyticsService(settings,market_provider=Mock(),artifact_store=Mock())
        control=FMPCurrentClassificationProvider(group_id_mapping_path=path)
        assert control.group_id_mapping.version=='audit-custom-v2'
        assert service.classification_provider.group_id_mapping.version!='audit-custom-v2'
        return {'configured_map':str(path),'direct_provider_version':control.group_id_mapping.version,'service_provider_version':service.classification_provider.group_id_mapping.version}

def optional_benchmark():
    from src.group_analytics.adapters import PublishedEODMarketDataProvider,PublishedMarketDataError
    from src.group_analytics.settings import GroupAnalyticsSettings
    from src.data.foundation import DataFoundationError
    class Reader:
        def require_latest(self,universe,**_):
            if universe!='SP500':raise DataFoundationError('synthetic missing optional benchmark publication')
            return SimpleNamespace(version_id='sp500-fixture')
        def load_bars(self,*_,**__):
            return pd.DataFrame({'date':pd.to_datetime(['2026-07-30','2026-07-31']),'ticker':['AAPL']*2,'adj_close':[100.0,101.0],'volume':[1e6]*2})
    assert GroupAnalyticsSettings().inputs.require_benchmark is False
    try:
        PublishedEODMarketDataProvider(reader=Reader()).snapshot(symbols=['AAPL'],benchmark='SPY',asof='2026-07-31')
    except PublishedMarketDataError as error:
        return {'configured_require_benchmark':False,'stocks_valid':True,'actual_error':str(error),'expected':'valid stock returns with unavailable benchmark'}
    raise AssertionError('expected optional-benchmark failure was not reproduced')

CHECKS={f.__name__:f for f in (rolling,outcomes,censored,watchlist,stale_quote,zero_cup_volume,mapping_ignored,optional_benchmark)}
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--only',choices=CHECKS)
    args=parser.parse_args()
    names=[args.only] if args.only else list(CHECKS)
    for name in names:
        try:
            print(json.dumps({'check':name,'result':CHECKS[name]()},ensure_ascii=False,allow_nan=False),flush=True)
        except Exception as error:
            print(json.dumps({'check':name,'unexpected_error':type(error).__name__+': '+str(error)},ensure_ascii=False),flush=True)
            raise
