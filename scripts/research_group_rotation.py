#!/usr/bin/env python3
"""Explicit research-only ETF data download and frozen-rule evaluation.

The download path uses dividend-adjusted complete OHLCV and is not the
production canonical close×volume amount path. A new study version is required
before comparing research scores with run snapshots field-for-field.
"""
from pathlib import Path
import argparse
import hashlib
import sys

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

import pandas as pd
from src.alerts.config import load_local_env
from src.group_analytics.calendar import _calendar, latest_completed_session
from src.group_analytics.adapters import _atomic_json
from src.group_analytics.rotation.store import encoded
from src.group_analytics.rotation.themes import Theme, default_themes, required_symbols
from src.group_analytics.rotation.validation import make_panel, summarize


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--env-file",type=Path)
    parser.add_argument("--download",action="store_true")
    args=parser.parse_args(argv)
    if args.env_file and load_local_env(args.env_file) is None: raise ValueError("Missing environment file")
    themes=tuple(t for t in default_themes() if t.proxy)
    end=latest_completed_session().date().isoformat()
    code_hashes = {name: hashlib.sha256((ROOT / "src/group_analytics/rotation" / name).read_bytes()).hexdigest()
                   for name in ("engine.py", "themes.py", "validation.py")}
    plan={"version":"rotation.validation.v1","start":"2019-01-01","end":end,
          "frozen_at":pd.Timestamp.now(tz="UTC").isoformat(),"themes":[t.record() for t in themes],
          "primary_horizon":20,"secondary_horizon":5,"split_years":[2023,2025],
          "baselines":["abs1","rs20","rs60","rs20_60","source_score","price_state"],
          "roundtrip_cost_bps":[0,10,25,50],"selection_bias":"retrospective-current-ETF-selection",
          "code_hashes":code_hashes}
    args.output.mkdir(parents=True,exist_ok=True)
    import json
    if (args.output/"plan.json").exists(): plan=json.loads((args.output/"plan.json").read_text())
    else: _atomic_json(args.output/"plan.json",plan)
    if plan.get("code_hashes") != code_hashes:
        raise ValueError("Frozen research code changed; use a new output/version, not this study")
    themes = tuple(Theme(**{**r,"members":tuple(r.get("members",()))}) for r in plan["themes"])
    frames={}; manifest=[]
    for symbol in required_symbols(themes):
        path=args.output/"prices"/f"{symbol}.parquet"
        if not path.exists() and args.download:
            from src.data.fmp import get_historical_ohlcv_complete
            frame=get_historical_ohlcv_complete(symbol,plan["start"],plan["end"],dividend_adjusted=True)
            if frame is None or frame.empty: raise ValueError(f"No research history for {symbol}")
            path.parent.mkdir(parents=True,exist_ok=True);frame.to_parquet(path)
        frames[symbol]=pd.read_parquet(path)
        manifest.append({"symbol":symbol,"rows":len(frames[symbol]),"first":str(frames[symbol].index.min()),
                         "last":str(frames[symbol].index.max()),"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
        print(f"Research input ready: {symbol} ({len(frames[symbol])} bars)",flush=True)
    inputs={"source":"FMP dividend-adjusted OHLC", "files":manifest}
    manifest_path=args.output/"inputs.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != inputs:
        raise ValueError("Frozen research input changed; refusing to overwrite evidence")
    _atomic_json(manifest_path,inputs)
    sessions=pd.DatetimeIndex(_calendar().sessions_in_range(plan["start"],plan["end"])).tz_localize(None)
    panel,prices,opens=make_panel(frames,sessions,themes)
    panel.to_parquet(args.output/"labels.parquet",index=False)
    report=summarize(panel,prices,opens,sessions,themes)
    report["plan_hash"]=hashlib.sha256(encoded(plan)).hexdigest()
    _atomic_json(args.output/"report.json",report)
    print(encoded({"status":report["status"],"dates":report["dates"],"promotion":report["promotion"],"paired_test":report["paired_test"]}).decode())
    return 0

if __name__=="__main__": raise SystemExit(main())
