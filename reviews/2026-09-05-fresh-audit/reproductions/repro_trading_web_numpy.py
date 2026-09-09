"""Isolated synthetic audit, no network or project data.

Until requirements installation completes, load actual numerical modules with
only CONFIG/logger substitutes; paper functions are compiled unchanged from
their AST with storage replaced by a private in-memory dict.
"""
from __future__ import annotations
import ast
from datetime import datetime, timezone
import importlib
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4
import numpy as np
import pandas as pd

ROOT = Path('/private/tmp/quant_fresh_audit_20260905/repo_trading')
OUT = Path('/private/tmp/quant_fresh_audit_20260905')

NORMAL = '--normal' in sys.argv
sys.path.insert(0,str(ROOT))
if not NORMAL:
    for name in ['src', 'src.backtest', 'src.utils', 'src.storage', 'src.papertrading', 'src.decision_replay']:
        mod = ModuleType(name)
        mod.__path__ = [str(ROOT.joinpath(*name.split('.')))]
        sys.modules[name] = mod
    config = ModuleType('src.config')
    config.PROJECT_ROOT = ROOT
    config.CONFIG = SimpleNamespace(backtest=SimpleNamespace(execution={}, trading_days_per_year=252, risk_free_rate=0))
    sys.modules[config.__name__] = config
    logger = ModuleType('src.utils.logger')
    logger.get_logger = logging.getLogger
    sys.modules[logger.__name__] = logger

from src.backtest.quintile_v2 import quintile_backtest_v2
from src.backtest.quintile import quintile_backtest
from src.backtest.double_sort import double_sort_backtest
from src.backtest.metrics import max_drawdown
from src.decision_replay.builder import build_backtest_snapshot
from src.execution import calculate_execution, max_volume_fill_quantity, max_buy_quantity_for_cash, resolve_execution_config
from src.storage.app_db import AppDatabase
from src.papertrading.notification_state import PaperNotificationState, KIND_DAILY_SUMMARY

execution = {'timing': 'next_open', 'portfolio_value': 1000., 'fee_model': 'simple_bps', 'commission_bps': 0., 'slippage_model': 'none', 'slippage_bps': 0.}
dates = pd.bdate_range('2026-01-05', periods=8)
factor = pd.DataFrame(np.tile(np.arange(4), (len(dates), 1)), index=dates, columns=list('ABCD'), dtype=float)
prices = factor * 0 + 100.
prices['A'] = [100,100,120,132,132,132,132,132]
kwargs = dict(n_groups=2, rebalance_days=3, rebalance_mode='every_n_days', tradable_mask=prices.notna(), execution=execution)
result = quintile_backtest_v2(factor, prices.pct_change(), execution_open_df=prices, execution_close_df=prices, total_return_open_df=prices, total_return_close_df=prices, benchmark_returns=pd.Series(0.,index=dates), **kwargs)
legacy = quintile_backtest(factor, prices.pct_change(), open_df=prices, price_df=prices, **kwargs)
checks = {'formal_drift': {'date': str(dates[2].date()), 'formal_return': result.group_daily_returns.loc[dates[2],'Q1'], 'self_financing_expected': (60/110)*.1, 'stateful_legacy_return': legacy.group_daily_returns.loc[dates[2],'Q1'], 'formal_position_rows': len(result.position_daily), 'formal_portfolio_rows': len(result.portfolio_daily), 'formal_rebalance_trades_after_drift': len(result.trades_detail.loc[pd.to_datetime(result.trades_detail.date).eq(dates[4])]), 'legacy_rebalance_trades_after_drift': len(legacy.trades_detail.loc[pd.to_datetime(legacy.trades_detail.date).eq(dates[4])])}}
assert abs(checks['formal_drift']['formal_return'] - .05) < 1e-10
assert abs(checks['formal_drift']['stateful_legacy_return'] - (60/110)*.1) < 1e-10
replay=build_backtest_snapshot(source_id='fresh-audit',strategy_snapshot={'components':[{'factor_id':'TEST','weight':1.}]},universe='SYNTHETIC',composite=factor,factor_raw={'TEST':factor},factor_clean={'TEST':factor},factor_inputs={'TEST':factor},factor_contributions={'TEST':factor},close_prices=prices,market_returns=prices.pct_change(),volumes=None,membership_mask=None,result=result,n_groups=2,top_group=1,normalized_weights={'TEST':1.},execution=execution)
checks['formal_drift']['replay_a_weight']=float(replay.portfolio['return_weights'].loc[dates[2],'A'])
checks['formal_drift']['replay_reported_audit_error']=replay.manifest['audit']['max_portfolio_contribution_error']
assert checks['formal_drift']['replay_a_weight']==.5
assert checks['formal_drift']['replay_reported_audit_error']==0.

flat = factor * 0 + 100.
cost_execution = {**execution,'commission_bps':10.}
cost_result = quintile_backtest_v2(factor, flat.pct_change(), execution_open_df=flat, execution_close_df=flat, total_return_open_df=flat, total_return_close_df=flat, benchmark_returns=pd.Series(0.,index=dates), **{**kwargs,'execution':cost_execution})
cost_date = cost_result.cost_returns.sum(axis=1).idxmax()
checks['formal_long_short_cost'] = {'date':str(cost_date.date()),'q1_cost':float(cost_result.cost_returns.loc[cost_date,'Q1']),'q2_cost':float(cost_result.cost_returns.loc[cost_date,'Q2']),'reported_ls':float(cost_result.long_short_returns.loc[cost_date]),'expected_ls':-float(cost_result.cost_returns.loc[cost_date].sum())}
assert checks['formal_long_short_cost']['reported_ls'] == 0
assert checks['formal_long_short_cost']['expected_ls'] < 0
try:
    double_sort_backtest(factor, factor, flat.pct_change(), n_control=1,n_factor=2,rebalance_days=3,rebalance_mode='every_n_days', execution=execution,tradable_mask=flat.notna(),execution_open_df=flat,execution_close_df=flat,total_return_open_df=flat,total_return_close_df=flat)
except ValueError as exc:
    checks['double_sort_explicit_prices'] = str(exc)
    assert 'Missing execution price' in str(exc)
else:
    raise AssertionError('Expected double-sort to fail')
checks['drawdown'] = {'returns':[-.1,-.1],'reported':max_drawdown(pd.Series([-.1,-.1])),'expected':-.19}

def load_functions(relative: str, env: dict, names: set[str] | None = None):
    if NORMAL:
        module=importlib.import_module(relative[:-3].replace('/','.'))
        # Keep actual production payload validation under the full runtime.
        if relative.endswith('/notifications.py'):env.pop('validate_discord_payload',None)
        module.__dict__.update(env)
        env.update(module.__dict__)
        return env
    path = ROOT / relative
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef,ast.ClassDef)) and (names is None or n.name in names)]
    unit = ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit),str(path),'exec'),env)
    return env

tables={}
paper = dict(np=np,pd=pd,Any=Any,datetime=datetime,uuid4=uuid4,calculate_execution=calculate_execution,max_volume_fill_quantity=max_volume_fill_quantity,max_buy_quantity_for_cash=max_buy_quantity_for_cash,resolve_execution_config=resolve_execution_config,now_iso=lambda:datetime.now().isoformat(),ORDER_FILLED='filled',ORDER_PENDING='pending',ORDER_REJECTED='rejected',load_table=lambda aid,name:tables.get(name,pd.DataFrame()).copy(),save_table=lambda aid,name,frame:tables.__setitem__(name,frame.copy()))
load_functions('src/papertrading/runner.py',paper)
tables['orders']=pd.DataFrame([{'order_id':'sell-a','ticker':'A','side':'SELL','decision_date':'2026-01-05','quantity':10,'status':'pending'},{'order_id':'buy-b','ticker':'B','side':'BUY','decision_date':'2026-01-05','quantity':10,'status':'pending'}])
target=SimpleNamespace(open_prices=pd.DataFrame({'A':[100,np.nan,100],'B':[100,100,100]},index=dates[:3]),volumes=pd.DataFrame())
cash,fills,orders=paper['_fill_pending_orders'](account={'id':'synthetic','execution':execution},target=target,cash=0.,positions_map={'A':{'quantity':10.,'avg_price':100.}},cutoff='2026-01-07')
timeline=sorted(fills,key=lambda f:f['fill_date'])
running_cash=0.; minimum_cash=0.
for fill in timeline:
    running_cash += fill['notional'] * (1 if fill['side']=='SELL' else -1) - fill['fee']
    minimum_cash=min(minimum_cash,running_cash)
checks['paper_future_cash']={'fills':[{k:f[k] for k in ['side','ticker','quantity','fill_date']} for f in fills],'minimum_cash_chronological':minimum_cash,'reported_final_cash':cash}
assert minimum_cash == -1000.

account={'id':'split','initial_cash':10000.}
historical_fills=pd.DataFrame([{'fill_id':'original','ticker':'A','side':'BUY','quantity':100.,'fill_price':100.,'notional':10000.,'fee':0.,'fill_date':'2026-01-05'}])
cash,positions=paper['_state_from_fill_ledger'](account,historical_fills)
_,before=paper['_mark_equity'](account_id='split',cash=cash,positions_map=positions,latest_prices=pd.Series({'A':100.}),mark_date='2026-01-05')
_,after=paper['_mark_equity'](account_id='split',cash=cash,positions_map=positions,latest_prices=pd.Series({'A':50.}),mark_date='2026-01-06')
checks['paper_split_version']={'old_equity':before,'new_split_adjusted_price':50.,'persisted_quantity':positions['A']['quantity'],'reported_equity':after,'economic_expected_after_2_for_1_split':10000.}
assert before==10000. and after==5000.

db_path=OUT / ('read_race_'+str(uuid4())+'.sqlite3')
db=AppDatabase(db_path)
db.put_frame('paper','test','equity',pd.DataFrame([{'equity':100.}]))
original_connect=db._connect
class Cursor:
    def __init__(self,inner): self.inner=inner
    def fetchone(self):
        value=self.inner.fetchone()
        db._connect=original_connect
        db.put_frame('paper','test','equity',pd.DataFrame([{'equity':101.},{'equity':102.}]))
        return value
class Connection:
    def __init__(self,inner): self.inner=inner
    def execute(self,sql,*args):
        cursor=self.inner.execute(sql,*args)
        return Cursor(cursor) if 'SELECT columns_json' in sql else cursor
    def close(self):self.inner.close()
db._connect=lambda:Connection(original_connect())
try:db.get_frame('paper','test','equity')
except ValueError as exc:
    checks['sqlite_snapshot_race']=str(exc)
    assert 'row count mismatch' in str(exc)
else:raise AssertionError('Expected false read-integrity error')
assert len(db.get_frame('paper','test','equity'))==2

from dataclasses import dataclass
import math
notice_env=dict(pd=pd,Any=Any,datetime=datetime,timezone=timezone,math=math,dataclass=dataclass,Path=Path,DEFAULT_STATE_PATH=OUT/'unused.sqlite3',PaperNotificationState=PaperNotificationState,KIND_DAILY_SUMMARY=KIND_DAILY_SUMMARY,validate_discord_payload=lambda x:x,list_accounts=lambda:[])
load_functions('src/papertrading/notifications.py',notice_env,{'_number','_money','_signed_money','build_daily_discord_payload','PaperNotificationService'})
state=PaperNotificationState(OUT/('notification_'+str(uuid4())+'.sqlite3'))
service=notice_env['PaperNotificationService'](SimpleNamespace(delivery_enabled=False,dashboard_base_url=''),state=state)
first=service.stage_daily_summary(target_session='2026-01-05')
try:service.stage_daily_summary(target_session='2026-01-05')
except RuntimeError as exc:
    checks['notification_same_day_retry']={'first':first,'second_error':str(exc)}
else:raise AssertionError('Expected timestamp identity collision')

(OUT/('repro_trading_web_normal_checkpoint.json' if NORMAL else 'repro_trading_web_numpy_checkpoint.json')).write_text(json.dumps(checks,indent=2,default=str))
if '--skip-web' not in sys.argv:
    route_env={'pd':pd}
    load_functions('src/webapp/routes.py',route_env,{'_json_records'})
    try:route_env['_json_records'](pd.DataFrame([{'ticker':'AAPL'}]))
    except NameError as exc:
        checks['web_factor_audit_json_import']=str(exc)
    else:raise AssertionError('Expected undefined json')

(OUT/('repro_trading_web_normal_result.json' if NORMAL else 'repro_trading_web_numpy_result.json')).write_text(json.dumps(checks,indent=2,default=str))
print(json.dumps(checks,indent=2,default=str))
