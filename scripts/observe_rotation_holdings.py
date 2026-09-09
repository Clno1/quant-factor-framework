#!/usr/bin/env python3
"""Explicit isolated current-ETF-holdings pilot; does not publish rotation or send."""
from pathlib import Path
import argparse
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

import pandas as pd
from src.alerts.config import load_local_env
from src.group_analytics.adapters import _atomic_json
from src.group_analytics.calendar import _calendar, latest_completed_session
from src.group_analytics.rotation.holdings import normalize_observation, observation_breadth
from src.group_analytics.rotation.store import encoded
from src.group_analytics.rotation.themes import default_themes


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--etf",default="SMH",choices=sorted(t.proxy for t in default_themes() if t.proxy))
    p.add_argument("--output",required=True,type=Path)
    p.add_argument("--env-file",type=Path)
    p.add_argument("--max-members",type=int,default=40)
    args=p.parse_args(argv)
    if args.env_file and load_local_env(args.env_file) is None: raise ValueError("Missing env file")
    # Refuse an occupied path so a retry cannot silently revise the snapshot.
    if args.output.exists(): raise ValueError("Use a new, empty observation output directory")
    from src.data.fmp import _get, get_historical_ohlcv
    rows=_get("/etf/holdings",{"symbol":args.etf})
    obs=normalize_observation(rows,args.etf,pd.Timestamp.now(tz="UTC"))
    if len(obs["members"])>args.max_members: raise ValueError("Member limit exceeded; increase explicitly")
    args.output.mkdir(parents=True)
    _atomic_json(args.output/"observation.json",obs)
    end=latest_completed_session()
    sessions=pd.DatetimeIndex(_calendar().sessions_in_range((end-pd.Timedelta(days=70)).date().isoformat(),end.date().isoformat())).tz_localize(None)
    frames={}; errors=[]
    for m in obs["members"]:
        symbol=m["ticker"]
        try:
            f=get_historical_ohlcv(symbol,sessions[0].date().isoformat(),end.date().isoformat(),dividend_adjusted=True)
            if f is not None and not f.empty:
                frames[symbol]=f
                (args.output/"prices").mkdir(exist_ok=True)
                f.to_parquet(args.output/"prices"/f"{symbol}.parquet")
        except Exception as exc:
            errors.append({"symbol":symbol,"error_type":type(exc).__name__})
    result=observation_breadth(obs,frames,sessions)
    result["download_errors"]=errors
    result["observation_sha256"]=obs["response_sha256"]
    _atomic_json(args.output/"report.json",result)
    print(encoded(result).decode())
    return 0


if __name__=="__main__": raise SystemExit(main())
