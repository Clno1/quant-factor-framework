"""Capture runtime evidence without changing observations or delivery state."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=False)
    status = json.loads(subprocess.check_output([sys.executable, str(ROOT / 'scripts/run_intraday_momentum_monitor.py'),
        '--env-file', '/etc/quant/intraday-momentum-monitor.env', '--status'], text=True, cwd=ROOT))
    (output / 'status.json').write_text(json.dumps(status, indent=2))
    from src.utils.env import load_local_env
    from src.breakouts.live.settings import IntradayMonitorSettings
    load_local_env('/etc/quant/intraday-momentum-monitor.env')
    assert IntradayMonitorSettings.load().cup_handle_delivery_enabled is False
    algo = 'daily-cup-5m-handle-shadow-v3'
    db = ROOT / 'outputs/intraday_momentum_monitor/state.sqlite3'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        tables = ['cup_handle_cycles', 'cup_handle_evaluations', 'cup_handle_session_observations', 'cup_handle_data_gaps']
        hashes = {}
        for name in tables:
            rows = conn.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
            hashes[name] = hashlib.sha256(json.dumps([list(r) for r in rows]).encode()).hexdigest()
        volumes = [dict(r) for r in conn.execute('SELECT * FROM cup_handle_evaluations WHERE algorithm_version=? AND rejection_reason=?',
                    (algo, 'INSUFFICIENT_VOLUME_EVIDENCE'))]
        for row in volumes:
            payload = json.loads(row['payload_json'])
            assert payload.get('signal') is None and row['outcome'] == 'REJECTED'
        gaps = [dict(r) for r in conn.execute('SELECT session_date,classification,count(*) AS events,count(DISTINCT ticker) AS tickers FROM cup_handle_data_gaps WHERE algorithm_version=? GROUP BY session_date,classification', (algo,))]
    (output / 'tables.json').write_text(json.dumps({'hashes': hashes, 'volume_rejections': volumes, 'gaps': gaps}, indent=2))
    services = ['quant-intraday-candidate-prepare', 'quant-intraday-momentum-monitor', 'quant-operations-watchdog', 'quant-operations-web']
    units = [n + '.service' for n in services] + [n + '.timer' for n in services[:3]]
    properties = ['ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'MemoryCurrent', 'MemoryPeak', 'CPUUsageNSec', 'TasksCurrent', 'NRestarts', 'NextElapseUSecRealtime', 'UnitFileState']
    resources = {u: subprocess.check_output(['systemctl', 'show', u, *['--property=' + p for p in properties]], text=True) for u in units}
    (output / 'services.json').write_text(json.dumps(resources, indent=2))
    for unit in [n + '.service' for n in services]:
        journal = subprocess.check_output(['journalctl', '-u', unit, '-n', '40', '--no-pager'], text=True)
        (output / (unit + '.log')).write_text(journal)
    import requests
    from src.operations_web.security import operations_credentials
    load_local_env('/etc/quant/operations-web.env')
    session = requests.Session()
    session.auth = operations_credentials()
    snapshots = {}
    for job in ['intraday_momentum', 'intraday_candidate_prepare', 'broad_us_pipeline']:
        response = session.get('http://127.0.0.1:18825/api/jobs/' + job, timeout=20)
        response.raise_for_status()
        value = response.json()
        (output / ('ops-' + job + '.json')).write_text(json.dumps(value, indent=2))
        snapshots[job] = value['snapshot']
    before = json.loads((ROOT / 'outputs/deployments/quant-isolation-release-20260913/live-tables-before.json').read_text())
    assert hashes == before, 'historical cup content changed since deployment'
    cup = snapshots['intraday_momentum']
    assert cup['stage'] == 'completed_session' and cup['status'] == 'DEGRADED'
    print(json.dumps({'cup_promotion': status['cup_handle_promotion'], 'delivery_enabled': False,
        'historical_tables_unchanged': True, 'ops': {k: {f: v.get(f) for f in ['status', 'stage', 'status_reason']} for k, v in snapshots.items()}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
