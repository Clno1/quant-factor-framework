"""Bounded raw-response experiment; no production market-data or ledger writes."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

import numpy as np
import pandas as pd
import requests

FIELDS = ["open", "high", "low", "close", "volume"]
TABLES = ["cup_handle_cycles", "cup_handle_evaluations",
          "cup_handle_session_observations", "cup_handle_data_gaps"]


def fingerprint(frame):
    return hashlib.sha256(frame.sort_index().to_json(orient="split", date_format="iso").encode()).hexdigest()


def day_frame(frame, session):
    return frame.loc[frame.index.strftime("%Y-%m-%d") == session, FIELDS].sort_index()


def values_equal(left, right):
    # A wider response can promote integer volume to float without changing values.
    return (left.index.equals(right.index) and list(left.columns) == list(right.columns)
            and bool(np.array_equal(left.to_numpy(), right.to_numpy(), equal_nan=True)))


def compare(one, five, session, shift=0):
    """Hypotheses only: shift one-minute labels, never mutate source timestamps."""
    one = one.copy()
    one.index += pd.Timedelta(minutes=shift)
    start, end = pd.Timestamp(session + " 09:30"), pd.Timestamp(session + " 16:00")
    one = one.loc[(one.index >= start) & (one.index < end)]
    five = five.loc[(five.index >= start) & (five.index < end)]
    duplicate = int(one.index.duplicated().sum() + five.index.duplicated().sum())
    if duplicate:
        return {"shift_minutes": shift, "duplicate_timestamps": duplicate, "comparable": False}
    grouped = one.groupby(one.index.floor("5min"))
    aggregate = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    counts = grouped.size()
    invalid = (~np.isfinite(one[FIELDS]).all(axis=1) | one[FIELDS].le(0).any(axis=1)
               | (one.index != one.index.floor("min"))
               | one.high.lt(one[["open", "low", "close"]].max(axis=1))
               | one.low.gt(one[["open", "high", "close"]].min(axis=1)))
    invalid_buckets = invalid.groupby(one.index.floor("5min")).any()
    shared = aggregate.index.intersection(five.index)
    valid_five = (np.isfinite(five[FIELDS]).all(axis=1) & five[FIELDS].gt(0).all(axis=1)
                  & (five.index == five.index.floor("5min"))
                  & five.high.ge(five[["open", "low", "close"]].max(axis=1))
                  & five.low.le(five[["open", "high", "close"]].min(axis=1)))
    valid = shared[~invalid_buckets.loc[shared] & valid_five.loc[shared]]
    complete = valid[counts.loc[valid].eq(5)]
    def mismatch(index):
        equal = np.isclose(aggregate.loc[index, FIELDS], five.loc[index, FIELDS], rtol=1e-9, atol=1e-9)
        return {"pairs": len(index), "all_ohlcv_equal": int(equal.all(axis=1).sum()),
                "different_by_field": {f: int((~equal[:, i]).sum()) for i, f in enumerate(FIELDS)}}
    grid = pd.date_range(start, end, freq="5min", inclusive="left")
    return {"shift_minutes": shift, "comparable": True, "one_rows": len(one), "five_rows": len(five),
            "observed_one_volume": float(one.volume.sum()), "observed_five_volume": float(five.volume.sum()),
            "one_missing_buckets": [str(t) for t in grid.difference(aggregate.index)],
            "five_missing_buckets": [str(t) for t in grid.difference(five.index)],
            "valid_observed": mismatch(valid), "complete_positive": mismatch(complete),
            "invalid_one_rows": int(invalid.sum()), "invalid_five_rows": int((~valid_five).sum()),
            "off_grid_one_rows": int((one.index != one.index.floor("min")).sum()),
            "off_grid_five_rows": int((five.index != five.index.floor("5min")).sum())}


def ledger(root):
    uri = (root / "outputs/intraday_momentum_monitor/state.sqlite3").as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        return {n: hashlib.sha256(json.dumps(con.execute("SELECT * FROM " + n + " ORDER BY rowid").fetchall()).encode()).hexdigest() for n in TABLES}


def collect(root, output, session):
    sys.path.insert(0, str(root))
    from src.data import fmp
    from src.utils.env import load_local_env
    from src.breakouts.live.settings import IntradayMonitorSettings
    load_local_env('/etc/quant/intraday-momentum-monitor.env')
    assert IntradayMonitorSettings.load().cup_handle_delivery_enabled is False
    output.mkdir(parents=True, exist_ok=False)
    before = ledger(root)
    (output / 'ledger-before.json').write_text(json.dumps(before, indent=2))
    metadata = []
    key = fmp.get_api_key()
    for variant in ('single', 'wide', 'repeat'):
        for ticker in ('UAN', 'IBTA', 'WBI', 'SPY'):
            for interval in ('1min', '5min'):
                start = (date.fromisoformat(session) - timedelta(days=7)).isoformat() if variant == 'wide' else session
                params = {'symbol': ticker, 'from': start, 'to': session}
                stem = f'{ticker}-{interval}-{variant}'
                row = {'ticker': ticker, 'interval': interval, 'variant': variant, 'params': params,
                       'observed_at': datetime.now(timezone.utc).isoformat()}
                try:
                    response = requests.get('https://financialmodelingprep.com/stable/historical-chart/' + interval,
                                            params={**params, 'apikey': key}, timeout=(10, 35), allow_redirects=False)
                    row['http_status'] = response.status_code
                    row['headers'] = {h: response.headers[h] for h in ('Date', 'Age', 'ETag', 'Last-Modified', 'Cache-Control', 'Content-Type') if h in response.headers}
                    raw = response.content
                    assert key.encode() not in raw, 'credential echoed in response'
                    row['raw_sha256'] = hashlib.sha256(raw).hexdigest()
                    row['raw_bytes'] = len(raw)
                    # Persist only successful record payloads, never credential-bearing URLs/errors.
                    response.raise_for_status()
                    payload = response.json()
                    assert isinstance(payload, list) and all(isinstance(r, dict) for r in payload)
                    (output / (stem + '.raw.json')).write_bytes(raw)
                    row['raw_rows'] = len(payload)
                    with patch.object(fmp, '_get', return_value=payload):
                        frame = fmp.get_intraday_ohlcv(ticker, interval=interval, start=start, end=session)
                    if frame is None:
                        row['status'] = 'EMPTY_NORMALIZED'
                    else:
                        frame.to_parquet(output / (stem + '.parquet'))
                        target = day_frame(frame, session)
                        row.update(status='RECEIVED', normalized_rows=len(frame), target_rows=len(target),
                                   target_sha256=fingerprint(target), parser_dropped_rows=len(payload)-len(frame),
                                   first_timestamp=str(frame.index.min()), last_timestamp=str(frame.index.max()),
                                   duplicate_timestamps=int(frame.index.duplicated().sum()))
                except Exception as exc:
                    row.update(status='FAILED', error_type=type(exc).__name__)
                metadata.append(row)
                (output / 'requests.json').write_text(json.dumps(metadata, indent=2))
                print(json.dumps({k: v for k, v in row.items() if k not in ('headers', 'params')}), flush=True)
                time.sleep(1)
    after = ledger(root)
    (output / 'ledger-after.json').write_text(json.dumps(after, indent=2))
    assert before == after, 'live ledger changed during experiment; inspect concurrent activity'
    assert IntradayMonitorSettings.load().cup_handle_delivery_enabled is False


def analyze(output, session, previous=None):
    requests_log = json.loads((output / 'requests.json').read_text())
    report = {'session': session, 'historical_classification_changed': False,
              'counts_for_shadow_promotion': False, 'delivery_enabled': False,
              'symbols': {}, 'requests': requests_log}
    for ticker in ('UAN', 'IBTA', 'WBI', 'SPY'):
        detail = {'request_consistency': {}, 'previous_comparison': {}, 'shift_hypotheses': []}
        for interval in ('1min', '5min'):
            rows = [r for r in requests_log if r['ticker'] == ticker and r['interval'] == interval]
            successful = len(rows)==3 and all(r['status']=='RECEIVED' for r in rows)
            frames = [day_frame(pd.read_parquet(output / f'{ticker}-{interval}-{v}.parquet'), session)
                      for v in ('single', 'wide', 'repeat')] if successful else []
            detail['request_consistency'][interval] = {'successful_variants': sum(r['status']=='RECEIVED' for r in rows),
                'same_target_values': successful and all(values_equal(frames[0], f) for f in frames[1:]),
                'same_serialized_hash': successful and len({r['target_sha256'] for r in rows})==1}
            if previous and (previous / f'{ticker}_{interval}.parquet').is_file():
                old = day_frame(pd.read_parquet(previous / f'{ticker}_{interval}.parquet'), session)
                current_path = output / f'{ticker}-{interval}-single.parquet'
                if current_path.is_file():
                    current = day_frame(pd.read_parquet(current_path), session)
                    detail['previous_comparison'][interval] = {'old_rows': len(old), 'new_rows': len(current),
                        'equal': values_equal(old, current), 'old_sha256': fingerprint(old), 'new_sha256': fingerprint(current)}
        paths = [output / f'{ticker}-{i}-single.parquet' for i in ('1min', '5min')]
        if all(p.is_file() for p in paths):
            one, five = [pd.read_parquet(p) for p in paths]
            detail['shift_hypotheses'] = [compare(one, five, session, shift) for shift in (-300,-240,-60,-5,-4,-1,0,1,4,5,60,240,300)]
        report['symbols'][ticker] = detail
    (output / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['collect', 'analyze'])
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--session', type=date.fromisoformat, required=True)
    p.add_argument('--previous', type=Path)
    args = p.parse_args()
    if args.mode == 'collect':
        collect(args.root.resolve(), args.output.resolve(), args.session.isoformat())
    else:
        analyze(args.output, args.session.isoformat(), args.previous)
