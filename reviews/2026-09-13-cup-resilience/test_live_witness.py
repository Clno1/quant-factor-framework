import importlib.util
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

spec=importlib.util.spec_from_file_location('witness',Path(__file__).with_name('live_witness.py'))
w=importlib.util.module_from_spec(spec); spec.loader.exec_module(w)
UTC=timezone.utc


def row(label='2026-09-14 09:30:00',volume=10):
    return dict(date=label,open=10,high=11,low=9,close=10,volume=volume)


def test_schedule_boundaries_budget_and_early_close():
    opens=datetime(2026,9,14,13,30,tzinfo=UTC)
    for minutes in (390,210):
        closes=opens+timedelta(minutes=minutes)
        s=w.slots(opens,closes)
        assert s[0]==opens-timedelta(seconds=15)
        assert s[-1]==closes+timedelta(hours=1)
        assert len(s)==len(set(s))
        assert 8*(len(s)+1)<=w.MAX_REQUESTS
        assert closes+timedelta(seconds=15) in s


@pytest.mark.parametrize('volume',[0,-1,float('nan'),float('inf')])
def test_invalid_volume_is_evidence_not_valid_bar(volume):
    rows,bad,dup=w.inspect_rows([row(volume=volume)],'2026-09-14','1min')
    assert not next(iter(rows.values()))['valid']
    json.dumps(rows,allow_nan=False)


def test_duplicate_and_off_grid_rows_not_certified():
    rows,bad,dup=w.inspect_rows([row(),row(),row('2026-09-14 09:31:01')],'2026-09-14','1min')
    assert dup and bad==['OFF_GRID'] and not next(iter(rows.values()))['valid']


def test_wrong_session_is_not_empty_no_trade_certificate():
    rows,bad,_=w.inspect_rows([row('2026-09-11 09:30:00')],'2026-09-14','1min')
    assert not rows and bad==['OTHER_SESSION']


def test_start_end_hypotheses_and_no_unfinished_comparison():
    one,_,_=w.inspect_rows([row(f'2026-09-14 09:{30+i}:00') for i in range(5)],'2026-09-14','1min')
    five,_,_=w.inspect_rows([row(volume=50)],'2026-09-14','5min')
    opens=datetime(2026,9,14,13,30,tzinfo=UTC); closes=opens+timedelta(hours=6,minutes=30)
    result=w.compare_pair(one,five,opens+timedelta(minutes=5),opens,closes)
    assert result[0]['equal_ohlcv_pairs']==1
    assert all(r['complete_positive_pairs']==0 for r in w.compare_pair(one,five,opens,opens,closes))
    del one[next(iter(one))]
    assert w.compare_pair(one,five,opens+timedelta(minutes=5),opens,closes)[0]['complete_positive_pairs']==0


class Response:
    status_code=200
    headers={'Date':'Mon, 14 Sep 2026 13:35:15 GMT'}
    def __init__(self,raw): self.raw=raw
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def iter_content(self,chunk_size): yield self.raw


def test_raw_capture_preserves_bytes_and_never_url(tmp_path,monkeypatch):
    monkeypatch.setenv('FMP_API_KEY','test-secret')
    (tmp_path/'raw').mkdir()
    raw=json.dumps([row()]).encode()
    r=w.request_record(tmp_path,'SPY','1min','2026-09-14','round','smoke',0,fetch=lambda *a,**k:Response(raw))
    assert r['status']=='RECEIVED' and r['raw_sha256']
    assert 'test-secret' not in (tmp_path/'requests.jsonl').read_text()


def test_error_does_not_leak_url(tmp_path,monkeypatch):
    monkeypatch.setenv('FMP_API_KEY','test-secret')
    def fail(*a,**k): raise RuntimeError('https://x?apikey=test-secret')
    r=w.request_record(tmp_path,'SPY','1min','2026-09-14','round','smoke',0,fetch=fail)
    assert r['status']=='FAILED'
    assert 'test-secret' not in (tmp_path/'requests.jsonl').read_text()


def test_credential_echo_body_is_not_saved(tmp_path,monkeypatch):
    monkeypatch.setenv('FMP_API_KEY','test-secret'); (tmp_path/'raw').mkdir()
    r=w.request_record(tmp_path,'SPY','1min','2026-09-14','round','smoke',0,fetch=lambda *a,**k:Response(b'test-secret'))
    assert r['status']=='FAILED' and not list((tmp_path/'raw').iterdir())


def test_recheck_cannot_overwrite_live_failure(tmp_path):
    w.status(tmp_path,'live',{'status':'FAILED'})
    w.status(tmp_path,'recheck',{'status':'CAPTURE_FINISHED'})
    result=json.loads((tmp_path/'status.json').read_text())
    assert result['live']['status']=='FAILED'
    assert result['recheck']['status']=='CAPTURE_FINISHED'


def test_revision_disappearance_and_no_automatic_approval(tmp_path,monkeypatch):
    monkeypatch.setenv('FMP_API_KEY','test-secret'); (tmp_path/'raw').mkdir()
    w.atomic(tmp_path/'plan.json',{'session':'2026-09-14','slots':[],
        'opens_at':'2026-09-14T13:30:00+00:00','closes_at':'2026-09-14T20:00:00+00:00'})
    for i,payload in enumerate(([row()], [row(volume=20)], [])):
        raw=json.dumps(payload).encode()
        w.request_record(tmp_path,'SPY','1min','2026-09-14',str(i),'smoke',0,
                         fetch=lambda *a,**k:Response(raw))
    w.analyze(tmp_path)
    result=json.loads((tmp_path/'analysis.json').read_text())
    assert {e['type'] for e in result['events']}=={'REVISION','DISAPPEARED_FROM_RESPONSE'}
    assert result['bars'][0]['revisions']==1
    assert result['feed_approved'] is False
    assert result['counts_for_shadow_promotion'] is False


def test_resource_skip_never_calls_provider(tmp_path,monkeypatch):
    monkeypatch.setenv('FMP_API_KEY','test-secret')
    monkeypatch.setattr(w,'guard',lambda root:None)
    monkeypatch.setattr(w,'clock_ok',lambda:True)
    monkeypatch.setattr(w,'resources_ok',lambda output:False)
    monkeypatch.setattr(w,'request_record',lambda *a,**k:pytest.fail('must not request'))
    plan={'session':'2026-09-14','slots':[],'recheck_at':'2026-09-15T12:00:00+00:00'}
    w.run(plan,tmp_path,'smoke',tmp_path)
    assert json.loads((tmp_path/'status-smoke.json').read_text())['status']=='CAPTURE_FINISHED_WITH_GAPS'


def test_past_session_cannot_be_backfilled_as_live(tmp_path,monkeypatch):
    monkeypatch.setenv('FMP_API_KEY','test-secret')
    monkeypatch.setattr(w,'guard',lambda root:None)
    monkeypatch.setattr(w,'clock_ok',lambda:True)
    monkeypatch.setattr(w,'request_record',lambda *a,**k:pytest.fail('must not request'))
    plan={'session':'2020-01-02','slots':['2020-01-02T14:30:00+00:00']}
    with pytest.raises(RuntimeError,match='outside scheduled'):
        w.run(plan,tmp_path,'live',tmp_path)
