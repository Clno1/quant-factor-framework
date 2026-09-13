"""Read current production evidence without publishing or changing shadow days."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.breakouts.live.candidates import build_daily_candidate_snapshot
from src.breakouts.live.settings import IntradayMonitorSettings
from src.config import CONFIG
from src.data.foundation import MarketDataCatalog, MarketDataReader
from src.operations.evidence import safe_text
from src.operations_web.security import operations_credentials
from src.utils.env import load_local_env
from scripts.build_us_liquid_pit import _parse_args, run


def command(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(safe_text(result.stderr, limit=2000))
    return result.stdout


def main():
    load_local_env('/etc/quant/intraday-momentum-monitor.env')
    settings = IntradayMonitorSettings.load()
    assert settings.cup_handle_delivery_enabled is False
    report = {"observed_at": datetime.now(timezone.utc).isoformat(),
        "cup_delivery_enabled": settings.cup_handle_delivery_enabled,
        "publication_requested": False, "live_observations_written": False}
    status = json.loads(command(sys.executable, 'scripts/run_intraday_momentum_monitor.py',
        '--env-file', '/etc/quant/intraday-momentum-monitor.env', '--status'))
    report['status'] = {key: status[key] for key in ('cup_handle_promotion', 'effective_cup_handle_mode')}
    names = ['quant-intraday-candidate-prepare', 'quant-intraday-momentum-monitor',
        'quant-operations-watchdog', 'quant-operations-web',
        'quant-coverage-repair-all-20260912', 'quant-coverage-revalidate-all-20260912']
    report['services'], report['timers'], report['journals'] = {}, {}, {}
    for name in names:
        text = command('systemctl', 'show', name+'.service',
            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'Result',
            '-p', 'ExecMainStatus', '-p', 'MemoryCurrent', '-p', 'MemoryPeak',
            '-p', 'CPUUsageNSec', '-p', 'NRestarts', '-p', 'TasksCurrent',
            '-p', 'ExecMainStartTimestamp', '-p', 'ExecMainExitTimestamp')
        report['services'][name] = dict(line.split('=', 1) for line in text.splitlines() if '=' in line)
        report['timers'][name] = command('systemctl', 'show', name+'.timer',
            '-p', 'LoadState', '-p', 'ActiveState', '-p', 'NextElapseUSecRealtime', '-p', 'LastTriggerUSec')
        report['journals'][name] = safe_text(command('journalctl', '-u', name+'.service',
            '--since', '2026-09-10', '-n', '35', '--no-pager', '-o', 'short-iso'), limit=18000)
    conn = sqlite3.connect('file:'+str(settings.state_path)+'?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    tables = ('cup_handle_cycles', 'cup_handle_evaluations', 'cup_handle_session_observations', 'cup_handle_data_gaps')
    report['tables'] = {}
    version = 'daily-cup-5m-handle-shadow-v3'
    for table in tables:
        rows = [dict(row) for row in conn.execute(f'SELECT * FROM {table} WHERE algorithm_version=? ORDER BY rowid', (version,))]
        report['tables'][table] = {'v3_rows': len(rows),
            'sha256': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}
        if table == 'cup_handle_session_observations':
            report['observations'] = rows
    conn.close()
    baseline = json.loads((Path(__file__).parent/'operations_checkpoint.json').read_text())
    report['v3_tables_unchanged'] = report['tables'] == baseline['tables']
    assert report['v3_tables_unchanged']
    load_local_env('/etc/quant/operations-web.env')
    credentials = operations_credentials()
    assert credentials
    headers = {'Authorization': 'Basic '+base64.b64encode(':'.join(credentials).encode()).decode()}
    for endpoint, key in [('/healthz', 'health'), ('/api/jobs/intraday_momentum', 'job')]:
        with urlopen(Request('http://127.0.0.1:18825'+endpoint, headers=headers), timeout=30) as response:
            data = json.load(response)
        if key == 'job':
            data = data['snapshot']
            data['metrics'] = {k: v for k, v in data.get('metrics', {}).items() if k.startswith('茶杯柄')}
        report[key] = data
    catalog = MarketDataCatalog(CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)))
    reader = MarketDataReader(catalog=catalog)
    coverage = reader.require_latest('US_EQUITY_COVERAGE', require_price_semantics=True)
    report['coverage'] = {'version_id': coverage.version_id,
        'manifest_sha256': coverage.manifest_checksum_sha256}
    checks = report['upstream_checks'] = {}
    calls = {
        'pit': lambda: run(_parse_args(['--dataset-version-id', coverage.version_id, '--full-rebuild', '--json'])),
        'candidate': lambda: build_daily_candidate_snapshot(settings, session_date='2026-09-14', source_session='2026-09-11'),
        'mdb_v3_replay': lambda: command(sys.executable, 'scripts/replay_cup_handle.py',
            '--ticker', 'MDB', '--end', '2026-08-11', '--output',
            str(ROOT/'outputs/data_audits/coverage_controlled_closeout'/f'mdb-{uuid4().hex}.json')),
    }
    for name, action in calls.items():
        try:
            result = action()
            checks[name] = {'status': 'CHECK_RETURNED', 'result': result}
        except Exception as exc:
            checks[name] = {'status': 'BLOCKED', 'error_type': type(exc).__name__, 'error': safe_text(str(exc), limit=2000)}
    output = ROOT/'outputs/data_audits/coverage_controlled_closeout'/f'{uuid4().hex}.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False, default=str)
    print(json.dumps({'report': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'status': report['status'], 'tables_unchanged': report['v3_tables_unchanged'],
        'coverage': report['coverage'], 'upstream_checks': checks}, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
