"""One-shot, resource-bounded migration using the published project commands."""
from pathlib import Path
from datetime import datetime,timezone
import json,subprocess,sys,time

ROOT=Path('/home/projects/quant')
PYTHON=ROOT/'.venv/bin/python'
TARGET='2026-09-04'
COMMIT='255e2755d75ba66fd06db469c95866b0e5b9b588'
RUN='nominal-migration-20260906'
REPORT_DIR=ROOT/'outputs/data_audits/broad_initial_rollout'/f'target={TARGET}'
REPORT_PATH=REPORT_DIR/f'run={RUN}.json'
LOG_DIR=ROOT/'logs'/RUN
REPORT_DIR.mkdir(parents=True,exist_ok=True);LOG_DIR.mkdir(parents=True,exist_ok=True)
report={'schema_version':1,'run_id':RUN,'target_session':TARGET,'code_commit':COMMIT,
        'status':'RUNNING','started_at':datetime.now(timezone.utc).isoformat(),'stages':[]}

def persist():
    report['updated_at']=datetime.now(timezone.utc).isoformat()
    temp=REPORT_PATH.with_suffix('.tmp');temp.write_text(json.dumps(report,indent=2)+'\n');temp.replace(REPORT_PATH)

def decode(text):
    decoder=json.JSONDecoder()
    for index in reversed([i for i,c in enumerate(text) if c=='{']):
        try: value,end=decoder.raw_decode(text[index:])
        except json.JSONDecodeError: continue
        if isinstance(value,dict) and not text[index+end:].strip(): return value
    raise RuntimeError('Stage did not return its JSON result.')

def stage(name,script,*args,accepted=(0,)):
    assert (ROOT/'.deploy-commit').read_text().strip()==COMMIT,'Code changed during migration.'
    report['current_stage']=name;persist()
    started=time.monotonic()
    command=[str(PYTHON),str(ROOT/'scripts'/script),*args,'--json']
    stdout=LOG_DIR/f'{name}.stdout.log';stderr=LOG_DIR/f'{name}.stderr.log'
    with stdout.open('w') as output,stderr.open('w') as error:
        process=subprocess.run(command,cwd=ROOT,stdout=output,stderr=error)
    result=None
    try: result=decode(stdout.read_text())
    except RuntimeError:
        if process.returncode==0: raise
    row={'name':name,'status':'SUCCESS' if process.returncode in accepted else 'FAILED',
         'returncode':process.returncode,'duration_seconds':round(time.monotonic()-started,3),
         'result':result,'stdout_path':str(stdout),'stderr_path':str(stderr),'command':command}
    report['stages'].append(row);persist()
    print(json.dumps({'stage':name,'status':row['status'],'returncode':process.returncode}),flush=True)
    if process.returncode not in accepted: raise RuntimeError(f'{name} failed with exit code {process.returncode}')
    return result

persist()
try:
    stage('RESOURCE_GUARD','check_broad_resources.py','--minimum-memory-mb','350','--minimum-disk-gb','15')
    coverage=stage('US_EQUITY_COVERAGE_BACKFILL','backfill_us_equity_coverage.py',
                   '--target-session',TARGET,'--history-start','2019-01-01',
                   '--workers','2','--batch-size','100','--auto-resume',
                   '--env-file','/etc/quant/market-data.env','--publish')
    coverage_id=coverage['publication']['version_id']
    pit=stage('US_LIQUID_5M_PIT','build_us_liquid_pit.py',
              '--dataset-version-id',coverage_id,'--full-rebuild','--publish')
    pit_id=pit['publication']['universe_version_id']
    stage('BROAD_FACTOR_DATA','run_broad_factor_data.py',
          '--dataset-version-id',coverage_id,'--universe-version-id',pit_id,
          '--full-rebuild','--auto-resume','--restart-after-partitions','1','--publish')
    readiness=stage('BROAD_RESEARCH_READINESS','check_broad_research_readiness.py',accepted=(0,2))
    if readiness.get('status')!='READY':
        blockers=set(readiness.get('blockers') or [])
        assert 'PIT_CLASSIFICATION_POLICY' in blockers and blockers<={'PIT_CLASSIFICATION_POLICY','PIT_INDUSTRY_COVERAGE'},'Unexpected broad research blocker.'
        report['expected_readiness_blockers']=sorted(blockers);persist()
    stage('BROAD_SHADOW_OBSERVATION','check_broad_shadow_observation.py','--record-current')
    stage('CORE_FACTOR_RESEARCH','run_factor_research.py',
          '--target-session',TARGET,'--universe','SP500','--universe','NASDAQ100',
          '--universe','MAG7','--force')
    report['status']='SUCCESS'
except Exception as error:
    report['status']='FAILED';report['error']=str(error)
finally:
    report['completed_at']=datetime.now(timezone.utc).isoformat();report['current_stage']=None;persist()
print(json.dumps({'status':report['status'],'report_path':str(REPORT_PATH)}),flush=True)
sys.exit(0 if report['status']=='SUCCESS' else 1)
