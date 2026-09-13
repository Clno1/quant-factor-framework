"""Isolated FMP arrival/revision witness. Never writes market data or shadow state."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import requests

UTC = timezone.utc
NY = ZoneInfo('America/New_York')
SYMBOLS = ('UAN', 'IBTA', 'WBI', 'SPY')
FIELDS = ('open', 'high', 'low', 'close', 'volume')
MAX_BODY = 2 * 1024 * 1024
MAX_REQUESTS = 2600


def stamp():
    return datetime.now(UTC).isoformat()


def parse(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError('clock timestamps must be timezone-aware')
    return result.astimezone(UTC)


def slots(opens, closes):
    if opens.tzinfo is None or closes.tzinfo is None or not 0 < (closes-opens).total_seconds() <= 23400:
        raise ValueError('invalid session')
    values = set()
    boundary = opens
    while boundary < closes:
        for offset in (-15, 15, 75, 180):
            values.add(boundary + timedelta(seconds=offset))
        boundary += timedelta(minutes=5)
    values.add(closes-timedelta(seconds=15))
    values.update(closes+timedelta(seconds=s) for s in (15,75,300,900,3600))
    return sorted(values)


def atomic(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, allow_nan=False))
    os.replace(temp, path)


def append(path, value):
    with path.open('a') as f:
        f.write(json.dumps(value, allow_nan=False) + '\n')
        f.flush()
        os.fsync(f.fileno())


def status(output, phase, value):
    atomic(output/f'status-{phase}.json', value)
    index=json.loads((output/'status.json').read_text()) if (output/'status.json').exists() else {}
    index[phase]=value
    atomic(output/'status.json',index)


def guard(root):
    from src.config import CONFIG
    from src.breakouts.live.settings import IntradayMonitorSettings
    CONFIG.reload(root/'configs/default.yaml')
    if IntradayMonitorSettings.load().cup_handle_delivery_enabled is not False:
        raise RuntimeError('cup delivery must remain disabled')


def clock_ok():
    result = subprocess.run(['timedatectl','show','-p','NTPSynchronized','--value'],
                            capture_output=True, text=True, timeout=5)
    return result.returncode == 0 and result.stdout.strip() == 'yes'


def resources_ok(output):
    values = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
    return (int(values['MemAvailable'].split()[0]) >= 300*1024
            and shutil.disk_usage(output).free >= 1024**3)


def request_record(output, ticker, interval, day, round_id, phase, order, fetch=requests.get):
    key = os.environ['FMP_API_KEY']
    started = datetime.now(UTC)
    mono = time.monotonic()
    record = {'ticker':ticker, 'interval':interval, 'session':day, 'round':round_id,
              'phase':phase, 'order':order, 'requested_at':started.isoformat()}
    try:
        # Single attempt, no redirect or HTTP retry. Pair skew is recorded, not hidden.
        with fetch('https://financialmodelingprep.com/stable/historical-chart/'+interval,
                   params={'symbol':ticker,'from':day,'to':day,'apikey':key},
                   timeout=(3,5), allow_redirects=False, stream=True) as response:
            record.update(http_status=response.status_code, headers_at=stamp(),
                          headers={h:response.headers[h] for h in ('Date','Age','ETag','Last-Modified','Cache-Control','Content-Type') if h in response.headers})
            if response.status_code != 200:
                record['status'] = 'HTTP_FAILED'
            else:
                parts, size = [], 0
                for part in response.iter_content(chunk_size=65536):
                    size += len(part)
                    if size > MAX_BODY or time.monotonic()-mono > 12:
                        raise ValueError('response limit')
                    parts.append(part)
                raw = b''.join(parts)
                record['received_at'] = stamp()
                if key.encode() in raw:
                    raise ValueError('credential echo')
                payload = json.loads(raw)
                if not isinstance(payload,list) or not all(isinstance(r,dict) for r in payload):
                    raise ValueError('not record payload')
                digest = hashlib.sha256(raw).hexdigest()
                target = output/'raw'/(digest+'.json.gz')
                if not target.exists():
                    target.write_bytes(gzip.compress(raw, mtime=0))
                record.update(status='RECEIVED', raw_sha256=digest, raw_bytes=len(raw), rows=len(payload))
    except Exception as exc:
        # Exception messages and request URLs may contain credentials.
        record.update(status='FAILED', error_type=type(exc).__name__)
    record.setdefault('received_at', stamp())
    record['elapsed_seconds'] = time.monotonic()-mono
    wall = (parse(record['received_at'])-started).total_seconds()
    record['clock_discontinuity'] = abs(wall-record['elapsed_seconds']) > 1.0
    append(output/'requests.jsonl',record)
    return record


def inspect_rows(payload, day, interval):
    """Retain invalid and duplicate evidence; it must not certify a completed bar."""
    rows, bad, duplicates = {}, [], []
    for row in payload:
        try:
            label = datetime.fromisoformat(row['date'])
            label = label.replace(tzinfo=NY) if label.tzinfo is None else label.astimezone(NY)
            if label.date().isoformat() != day:
                bad.append('OTHER_SESSION'); continue
            if label.second or label.microsecond or (interval=='5min' and label.minute%5):
                bad.append('OFF_GRID'); continue
            values = [float(row[f]) for f in FIELDS]
            valid = (all(math.isfinite(v) and v>0 for v in values)
                     and values[1] >= max(values[0],values[2],values[3])
                     and values[2] <= min(values[0],values[1],values[3]))
            key = label.isoformat()
            if key in rows:
                duplicates.append(key)
            rows[key] = {'values':values if all(math.isfinite(v) for v in values) else None,
                         'valid':valid, 'label_utc':label.astimezone(UTC).isoformat()}
        except (ValueError,TypeError,KeyError,OverflowError):
            bad.append('INVALID_ROW')
    for key in duplicates:
        rows[key]['valid'] = False
    return rows, bad, duplicates


def compare_pair(one, five, observed_before, opens, closes):
    results=[]
    for minute_end_label in (False,True):
        for five_end_label in (False,True):
            complete=equal=0; differences=[]
            for label,native in five.items():
                end=parse(label) if five_end_label else parse(label)+timedelta(minutes=5)
                start=end-timedelta(minutes=5)
                if start<opens or end>closes or end>observed_before or not native['valid']: continue
                keys=[(start+timedelta(minutes=i+int(minute_end_label))).astimezone(NY).isoformat() for i in range(5)]
                if not all(k in one and one[k]['valid'] for k in keys): continue
                values=[one[k]['values'] for k in keys]
                aggregate=[values[0][0],max(v[1] for v in values),min(v[2] for v in values),values[-1][3],sum(v[4] for v in values)]
                different=[f for i,f in enumerate(FIELDS) if not math.isclose(aggregate[i],native['values'][i],rel_tol=1e-9,abs_tol=1e-9)]
                complete+=1; equal+=int(not different)
                if different: differences.append({'label':label,'fields':different})
            results.append({'minute_end_label':minute_end_label,'five_end_label':five_end_label,
                            'complete_positive_pairs':complete,'equal_ohlcv_pairs':equal,'differences':differences})
    return results


def analyze(output):
    plan = json.loads((output/'plan.json').read_text())
    histories, latest, events, pair_times, pending, comparisons = {}, {}, [], {}, {}, []
    counts = {'requests':0,'received':0,'failed':0,'invalid_rows':0,'duplicates':0,'clock_discontinuities':0}
    request_lines=(output/'requests.jsonl').read_text().splitlines() if (output/'requests.jsonl').exists() else []
    for line in request_lines:
        r=json.loads(line); counts['requests']+=1
        if r['status']!='RECEIVED': counts['failed']+=1; continue
        counts['received']+=1
        if r['clock_discontinuity']: counts['clock_discontinuities']+=1
        raw=gzip.decompress((output/'raw'/(r['raw_sha256']+'.json.gz')).read_bytes())
        if hashlib.sha256(raw).hexdigest()!=r['raw_sha256']: raise ValueError('raw checksum mismatch')
        rows,bad,duplicates=inspect_rows(json.loads(raw),r['session'],r['interval'])
        counts['invalid_rows']+=len(bad)+sum(not row['valid'] for row in rows.values())
        counts['duplicates']+=len(duplicates)
        pair_times.setdefault((r['phase'],r['round'],r['ticker']),{})[r['interval']]=r['received_at']
        pair_key=(r['phase'],r['round'],r['ticker'])
        pending.setdefault(pair_key,{})[r['interval']]=(r,rows)
        if len(pending[pair_key])==2:
            first,second=pending.pop(pair_key).values()
            by_interval={first[0]['interval']:first,second[0]['interval']:second}
            if not first[0]['clock_discontinuity'] and not second[0]['clock_discontinuity']:
                comparisons.append({'phase':pair_key[0],'round':pair_key[1],'ticker':pair_key[2],
                    'hypotheses':compare_pair(by_interval['1min'][1],by_interval['5min'][1],
                        min(parse(first[0]['requested_at']),parse(second[0]['requested_at'])),
                        parse(plan['opens_at']),parse(plan['closes_at']))})
        # Failed half-pairs must not accumulate payloads for a whole session.
        pending={k:v for k,v in pending.items() if k[:2]==pair_key[:2]}
        series=(r['ticker'],r['interval'])
        for missing in latest.get(series,set())-rows.keys():
            events.append({'type':'DISAPPEARED_FROM_RESPONSE','ticker':r['ticker'],'interval':r['interval'],
                           'label':missing,'observed_at':r['received_at'],'phase':r['phase']})
        latest[series]=set(rows)
        for label,row in rows.items():
            key=series+(label,)
            h=histories.setdefault(key,{'ticker':r['ticker'],'interval':r['interval'],'label':label,
                'first_seen_at':r['received_at'],'first_valid_seen_at':None,'first_phase':r['phase'],
                'last_change_at':r['received_at'],'revisions':0,'last_values':row['values'],'last_valid':row['valid']})
            if row['valid'] and not r['clock_discontinuity'] and h['first_valid_seen_at'] is None:
                h['first_valid_seen_at']=r['received_at']
            if h['last_values']!=row['values'] or h['last_valid']!=row['valid']:
                events.append({'type':'REVISION','ticker':r['ticker'],'interval':r['interval'],'label':label,
                    'previous':h['last_values'],'current':row['values'],'previous_valid':h['last_valid'],
                    'current_valid':row['valid'],'observed_at':r['received_at'],'phase':r['phase']})
                h['revisions']+=1; h['last_change_at']=r['received_at']
            h.update(last_values=row['values'],last_valid=row['valid'],last_seen_at=r['received_at'])
    bars=[]
    for h in histories.values():
        label=parse(h['label']); duration=60 if h['interval']=='1min' else 300
        if h['first_valid_seen_at']:
            h['first_valid_delay_if_start_label_seconds']=(parse(h['first_valid_seen_at'])-label).total_seconds()-duration
            h['first_valid_delay_if_end_label_seconds']=(parse(h['first_valid_seen_at'])-label).total_seconds()
        h['label_semantics']='UNVERIFIED_HYPOTHESES'
        bars.append(h)
    pairs=[{'phase':k[0],'round':k[1],'ticker':k[2],
            'receipt_skew_seconds':abs((parse(v['1min'])-parse(v['5min'])).total_seconds())}
           for k,v in pair_times.items() if set(v)=={'1min','5min'}]
    rounds=[json.loads(line) for line in (output/'rounds.jsonl').open()] if (output/'rounds.jsonl').exists() else []
    result={'version':'minute-live-witness-v1','session':plan['session'],'generated_at':stamp(),
        'counts':counts,'planned_live_rounds':len(plan['slots']), 'observed_rounds':rounds,
        'paired_responses':pairs,'paired_ohlcv_comparisons':comparisons,'bars':bars,'events':events,
        'interpretation':'First receipt is an observation bound, not exact provider publication time. Stable observations do not certify finality.',
        'counts_for_shadow_promotion':False,'feed_approved':False,'historical_classification_changed':False}
    atomic(output/'analysis.json',result)
    print(json.dumps({'analysis':str(output/'analysis.json'),'counts':counts,'events':len(events),'feed_approved':False}))


def run(plan,output,mode,root):
    output.mkdir(parents=True,exist_ok=True); (output/'raw').mkdir(exist_ok=True)
    guard(root)
    if not clock_ok(): raise RuntimeError('host clock not synchronized')
    key=os.environ.get('FMP_API_KEY','').strip()
    if not key: raise RuntimeError('FMP_API_KEY missing')
    os.environ['FMP_API_KEY']=key
    if output.joinpath('plan.json').exists():
        if json.loads((output/'plan.json').read_text()) != plan: raise ValueError('frozen plan changed')
    else: atomic(output/'plan.json',plan)
    if mode=='check':
        print(json.dumps({'ready':True,'delivery_enabled':False,'slots':len(plan['slots']),
                          'max_planned_requests':8*(len(plan['slots'])+1),'clock_synchronized':True})); return
    schedule=[parse(t) for t in plan['slots']] if mode=='live' else [parse(plan['recheck_at'])]
    if mode=='smoke': schedule=[datetime.now(UTC)]
    start=datetime.now(UTC)
    if mode!='smoke' and (start < schedule[0]-timedelta(seconds=60) or start > schedule[-1]+timedelta(seconds=20)):
        raise RuntimeError('outside scheduled capture window; no historical catch-up')
    request_count=sum(1 for _ in (output/'requests.jsonl').open()) if (output/'requests.jsonl').exists() else 0
    errors=0; used=sum(p.stat().st_size for p in output.rglob('*') if p.is_file())
    completed=set()
    if (output/'rounds.jsonl').exists():
        completed={(r['phase'],r['round']) for r in map(json.loads,(output/'rounds.jsonl').read_text().splitlines())}
    for index,target in enumerate(schedule):
        round_id=target.isoformat()
        if (mode,round_id) in completed: continue
        delay=(target-datetime.now(UTC)).total_seconds()
        if delay>0: time.sleep(delay)
        late=(datetime.now(UTC)-target).total_seconds()
        if late>20:
            append(output/'rounds.jsonl',{'phase':mode,'round':round_id,'status':'MISSED_SLOT','lateness_seconds':late}); continue
        guard(root)
        if not resources_ok(output):
            append(output/'rounds.jsonl',{'phase':mode,'round':round_id,'status':'RESOURCE_SKIPPED'}); continue
        if index%40==0 and not clock_ok(): raise RuntimeError('clock synchronization lost')
        intervals=('1min','5min') if index%2==0 else ('5min','1min')
        statuses=[]
        for ticker in SYMBOLS:
            for order,interval in enumerate(intervals):
                if request_count>=MAX_REQUESTS or used>=256*1024**2: raise RuntimeError('witness budget exhausted')
                r=request_record(output,ticker,interval,plan['session'],round_id,mode,order)
                request_count+=1; used+=r.get('raw_bytes',0); statuses.append(r['status'])
                errors=errors+1 if r['status']!='RECEIVED' else 0
                if r.get('http_status') in (401,402,403,429) or errors>=3 or r['clock_discontinuity']:
                    raise RuntimeError('witness circuit breaker; inspect recorded evidence')
                time.sleep(1)
        append(output/'rounds.jsonl',{'phase':mode,'round':round_id,'status':'CAPTURED',
                                     'received':statuses.count('RECEIVED'),'finished_at':stamp()})
        status(output,mode,{'phase':mode,'status':'RUNNING','updated_at':stamp(),'requests':request_count,'last_round':round_id})
    analyze(output)
    records=[json.loads(line) for line in (output/'rounds.jsonl').read_text().splitlines()]
    captured=sum(r['phase']==mode and r['status']=='CAPTURED' and r.get('received')==8 for r in records)
    status(output,mode,{'phase':mode,'status':'CAPTURE_FINISHED' if captured==len(schedule) else 'CAPTURE_FINISHED_WITH_GAPS',
                       'captured_rounds':captured,'expected_rounds':len(schedule),'updated_at':stamp(),'requests':request_count,
                       'feed_approved':False,'counts_for_shadow_promotion':False})


def main():
    def stopped(signum, frame):
        raise RuntimeError('capture interrupted')
    signal.signal(signal.SIGTERM, stopped)
    signal.signal(signal.SIGINT, stopped)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['plan','check','smoke','live','recheck','analyze'])
    p.add_argument('--root',type=Path,required=True)
    p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--output',type=Path)
    p.add_argument('--session')
    args=p.parse_args(); root=args.root.resolve(); sys.path.insert(0,str(root))
    if args.mode=='plan':
        from src.breakouts.live.session import xnys_session_schedule
        from src.breakouts.live.session import _exchange_calendar
        schedule=xnys_session_schedule(args.session)
        opens,closes=schedule.opens_at.astimezone(UTC),schedule.closes_at.astimezone(UTC)
        next_day=_exchange_calendar().next_session(args.session).strftime('%Y-%m-%d')
        next_schedule=xnys_session_schedule(next_day)
        plan={'version':'minute-live-witness-v1','session':args.session,'opens_at':opens.isoformat(),
              'closes_at':closes.isoformat(),'symbols':list(SYMBOLS),'slots':[t.isoformat() for t in slots(opens,closes)],
              'recheck_at':(next_schedule.opens_at.astimezone(UTC)-timedelta(minutes=90)).isoformat(),
              'counts_for_shadow_promotion':False,'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if args.plan.exists(): raise FileExistsError(args.plan)
        atomic(args.plan,plan); print(json.dumps(plan)); return
    plan=json.loads(args.plan.read_text()); output=args.output.resolve()
    if plan['symbols']!=list(SYMBOLS) or len(plan['slots'])*8+8>MAX_REQUESTS: raise ValueError('invalid request scope')
    if args.mode=='analyze': analyze(output); return
    if plan['source_sha256']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(): raise ValueError('source changed after freeze')
    output.mkdir(parents=True,exist_ok=True)
    with (output/'.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try: run(plan,output,args.mode,root)
        except BaseException as exc:
            status(output,args.mode,{'phase':args.mode,'status':'FAILED','updated_at':stamp(),'error_type':type(exc).__name__,
                                    'feed_approved':False,'counts_for_shadow_promotion':False})
            print(json.dumps({'status':'FAILED','error_type':type(exc).__name__}),file=sys.stderr)
            raise SystemExit(2)


if __name__=='__main__':
    main()
