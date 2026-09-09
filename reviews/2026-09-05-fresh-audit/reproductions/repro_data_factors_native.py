"""Native imports, real Parquet/DuckDB, synthetic local inputs only."""
from __future__ import annotations
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import pandas as pd

ROOT=Path('/private/tmp/quant_fresh_audit_20260905/repo_data')
sys.path.insert(0,str(ROOT))
from src.data.derived_universe import build_liquid_5m_candidate,roll_forward_liquid_5m_candidate
from src.data.foundation import DataFoundationError,MarketDataCatalog,MarketDataReader,MarketDataWriter
from src.data.broad_coverage import BroadCoverageStore,BroadCoverageReader,normalize_coverage_bars,split_coverage_bar_quality
from src.data.price_semantics import PriceSemantics,build_price_semantics_contract
from src.data.security_master_store import SecurityMasterGeneration
from src.data.pit import build_membership_mask
import exchange_calendars as xcals

cal=xcals.get_calendar('XNYS')
dates=pd.DatetimeIndex(cal.sessions_in_range('2024-01-01','2024-02-23')).tz_localize(None)
master=pd.DataFrame([dict(security_id=sid,current_ticker=ticker,asset_type='STOCK',primary_exchange='NYSE',
    listing_date='2020-01-01',delisting_date=None,trading_status='ACTIVE') for sid,ticker in [('s1','AAA'),('s2','BBB')]])
bars=pd.DataFrame([dict(date=dt,security_id=sid,ticker=ticker,close=price,volume=volume)
    for dt in dates for sid,ticker,price,volume in [('s1','AAA',2.,3_000_000),('s2','BBB',10.,1_000_000)]])
kw=dict(parent_version_id='v1',history_start='2024-01-01',research_start='2024-02-01',
        adv_sessions=2,min_valid_sessions=2,calendar=cal)
previous=build_liquid_5m_candidate(bars,master,target_session='2024-02-22',**kw)
result={}
try:
    roll_forward_liquid_5m_candidate(previous.membership,previous.eligibility,master,
        previous_target_session='2024-02-22',target_session='2024-02-23',refresh_start='2024-02-01',
        bar_loader=lambda start,end:bars.loc[bars.date.between(start,end)],**kw)
    raise AssertionError('Expected no-event defect did not reproduce')
except DataFoundationError as exc:
    result['no_event_rollforward']={'error':str(exc),'prior_passed':previous.passed,
        'full_rebuild_passed':build_liquid_5m_candidate(bars,master,target_session='2024-02-23',**kw).passed}
split=bars.copy()
split.loc[split.security_id.eq('s1'),'close']/=10
split.loc[split.security_id.eq('s1'),'volume']*=10
after=build_liquid_5m_candidate(split,master,target_session='2024-02-22',**kw)
result['future_split_pit']={'before':previous.membership.ticker.tolist(),'after':after.membership.ticker.tolist()}
assert 'AAA' in result['future_split_pit']['before'] and 'AAA' not in result['future_split_pit']['after']

with tempfile.TemporaryDirectory(prefix='native_data_',dir='/private/tmp/quant_fresh_audit_20260905') as tmp:
    tmp=Path(tmp)
    catalog=MarketDataCatalog(tmp/'catalog.duckdb')
    store=BroadCoverageStore(catalog=catalog,lake_dir=tmp/'lake')
    universe=master.iloc[:1].copy()
    universe['ticker']='AAA';universe['name']='Alpha';universe['coverage_role']='EQUITY';universe['is_current_coverage']=True
    universe['listing_date']=pd.to_datetime(universe.listing_date);universe['delisting_date']=pd.to_datetime(universe.delisting_date)
    gen=SecurityMasterGeneration(generation_id='s1',target_session=pd.Timestamp('2024-02-01').date(),
        created_at=datetime.now(timezone.utc),status='PUBLISHED',row_count=1,active_count=1,
        master_path='master.parquet',symbols_path='symbols.parquet',classifications_path='classifications.parquet',
        identity_keys_path='keys.parquet',manifest_path='manifest.json',master_sha256='m',symbols_sha256='s',
        classifications_sha256='c',identity_keys_sha256='i',manifest_sha256='manifest')
    history=pd.DataFrame([dict(date='2024-01-31',security_id='s1',ticker='AAA',
        open=100.,high=100.,low=100.,close=100.,adj_close=100.,volume=1_000_000.)])
    original=normalize_coverage_bars(history,target_session=pd.Timestamp('2024-01-31'),ingestion_run_id='old')
    old_path=tmp/'old.parquet';original.to_parquet(old_path,index=False)
    parent=store.publish_partitions([old_path],security_universe=universe,target_session='2024-01-31',security_master=gen,
        price_semantics=build_price_semantics_contract(source='TEST_CANONICAL_FIXTURE',history_mode='FULL_REBUILD'))
    delta=normalize_coverage_bars(pd.DataFrame([dict(date='2024-02-01',security_id='s1',ticker='AAA',
        open=50.,high=50.,low=50.,close=50.,adj_close=50.,volume=2_000_000.)]),
        target_session=pd.Timestamp('2024-02-01'),ingestion_run_id='new')
    combined=pd.concat([original,delta],ignore_index=True).drop_duplicates(['date','security_id'],keep='last').sort_values(['date','security_id']).reset_index(drop=True)
    accepted,quarantine=split_coverage_bar_quality(combined)
    candidate=tmp/'candidate.parquet';accepted.to_parquet(candidate,index=False)
    checks,stats=store._validate_partitions([candidate],security_universe=universe,target_session=pd.Timestamp('2024-02-01'),min_target_coverage=.98)
    child=store.publish_partitions([candidate],security_universe=universe,target_session='2024-02-01',security_master=gen,
        price_semantics=build_price_semantics_contract(source='TEST_CANONICAL_FIXTURE',history_mode='INCREMENTAL_FROM_AUTHENTICATED_PARENT'),
        price_semantics_parent_version_id=parent.version.version_id)
    loaded=BroadCoverageReader(market_reader=MarketDataReader(catalog=catalog)).load_bars(start='2024-01-31',end='2024-02-01',version=child.version)
    wide={c:loaded.pivot(index='date',columns='ticker',values=c) for c in ['open','close','adj_close','volume']}
    result['split_published_after_actual_gates']={'all_checks_passed':all(c.passed for c in checks),
        'checks':[c.name for c in checks],'quarantined_rows':len(quarantine),'published':child.version.status,
        'wrong_total_return':float(PriceSemantics.from_wide(wide).total_returns.iloc[-1,0])}
    assert result['split_published_after_actual_gates']['wrong_total_return']==-.5

    from scripts.run_broad_factor_data import _checkpoint_identity,_load_checkpoint
    from src.factors.broad_pipeline import factor_input_fingerprint
    from src.config import CONFIG
    identity_args=dict(generation_id='factor-g1',parent=child.version,
        universe_version=SimpleNamespace(universe_version_id='u1',membership_sha256='m1',eligibility_sha256='e1'),
        security_generation=gen,factors=['MOM_1M'],start=pd.Timestamp('2024-01-31'),reuse_publication_id=None)
    fingerprint_args=dict(factor_id='MOM_1M',parent_version=child.version,
        membership=pd.DataFrame([dict(date=pd.Timestamp('2024-01-31'),security_id='s1',ticker='AAA',active=True,snapshot_type='MONTH_END')]),
        classifications=pd.DataFrame(),output_start='2024-01-31',output_end='2024-02-01')
    old_n=CONFIG['preprocessing']['winsorize_n']
    try:
        CONFIG['preprocessing']['winsorize_n']=3.
        id1=_checkpoint_identity(**identity_args)
        fp1,_=factor_input_fingerprint(**fingerprint_args)
        cp=tmp/'checkpoint.json'
        cp.write_text(json.dumps({**id1,'completed':{'MOM_1M:2024-01':{'old_fingerprint':fp1}}}))
        CONFIG['preprocessing']['winsorize_n']=1.
        id2=_checkpoint_identity(**identity_args)
        fp2,_=factor_input_fingerprint(**fingerprint_args)
        accepted_checkpoint=_load_checkpoint(cp,id2)
        result['resume_changed_preprocessing']={'checkpoint_identity_equal':id1==id2,
            'actual_factor_fingerprint_changed':fp1!=fp2,'old_completed_accepted':bool(accepted_checkpoint['completed'])}
        assert id1==id2 and fp1!=fp2 and accepted_checkpoint['completed']
    finally:
        CONFIG['preprocessing']['winsorize_n']=old_n

    mag7=['AAPL','AMZN','GOOGL','META','MSFT','NVDA','TSLA']
    def fetcher(ticker,start,end):
        index=pd.DatetimeIndex(cal.sessions_in_range(start,end)).tz_localize(None)
        return pd.DataFrame({**{c:10. for c in ['open','high','low','close','adj_close']},'volume':1_000_000.},index=index)
    writer=MarketDataWriter(catalog=catalog,lake_dir=tmp/'lake',fetcher=fetcher,fetcher_semantics_source='TEST_CANONICAL_FIXTURE')
    mag=writer.update_universe('MAG7',target_session='2024-02-01',initial_start='2024-01-30',
        universe_frame=pd.DataFrame({'ticker':mag7,'name':mag7,'sector':'Technology'}),workers=1)
    reader=MarketDataReader(catalog=catalog)
    wide=reader.load_wide_tables('MAG7',version=mag.version)
    membership=reader.load_membership('MAG7',version=mag.version)
    mask,_=build_membership_mask(wide['adj_close'].index,wide['adj_close'].columns,'MAG7',membership_override=membership)
    if mask is None:mask=pd.DataFrame(True,index=wide['adj_close'].index,columns=wide['adj_close'].columns)
    result['mag7_real_publication']={'membership_none':membership is None,'actual_factor_columns':wide['adj_close'].columns.tolist(),
        'QQQ_mask_true':bool(mask['QQQ'].all()),'current_only':reader.load_universe('MAG7',version=mag.version,current_only=True).ticker.tolist()}
    assert len(wide['adj_close'].columns)==8 and len(result['mag7_real_publication']['current_only'])==7

print(json.dumps(result,indent=2,ensure_ascii=False,default=str))
