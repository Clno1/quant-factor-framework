"""Activate the tested SG session guard without changing budgets or routing."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

HOME = Path('/home/projects/quant')
BASE = HOME / 'releases/ep-consumers-0329a78620e7'
TARGET = BASE.with_name(BASE.name + '-sessionguard')
LINK = HOME / 'ep-event-current'
WORK = HOME / 'data/ep/consumer-rollout-ep-consumers-0329a78620e7'
TIMER = 'quant-ep-event-review.timer'
SERVICE = 'quant-ep-event-review-root.service'


def ctl(*args):
    return subprocess.check_output(['systemctl', *args], text=True).strip()


def counters(config):
    result = {}
    for key, query in [
        ('database', 'SELECT COUNT(*), COALESCE(SUM(reserved_microusd),0) FROM ep_llm_calls'),
        ('outbox_database', 'SELECT state,COUNT(*) FROM ep_ai_outbox GROUP BY state'),
    ]:
        with sqlite3.connect(Path(config[key]).as_uri() + '?mode=ro', uri=True) as db:
            result[key] = db.execute(query).fetchall()
    return result


def point(target):
    temporary = LINK.with_name('ep-guard-next-' + os.urandom(4).hex())
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, LINK)


def main():
    if os.geteuid() != 0 or LINK.resolve() != BASE:
        raise RuntimeError('UNEXPECTED_HOST_OR_RELEASE')
    manifest = json.loads((TARGET / 'ep_release_manifest.json').read_text())
    for name, expected in manifest['files'].items():
        path = TARGET / name
        if path.is_symlink() or not path.resolve().is_relative_to(TARGET):
            raise RuntimeError('INVALID_MANIFEST_PATH')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError('MANIFEST_MISMATCH')
    sys.path.insert(0, str(TARGET))
    from src.breakouts.ep.consumers import market_window
    if market_window(datetime.now(timezone.utc)):
        raise RuntimeError('OFF_MARKET_CUTOVER_REQUIRED')
    for unit in [TIMER, SERVICE] + [f'quant-ep-consumer@{lane}.service' for lane in ('price', 'news', 'source')]:
        if ctl('show', unit, '-p', 'ActiveState', '--value') != 'inactive':
            raise RuntimeError('WORKER_NOT_IDLE: ' + unit)
    config_path = Path('/etc/quant/ep-event-worker.json')
    config_bytes = config_path.read_bytes()
    config = json.loads(config_bytes)
    before = counters(config)
    receipt = {'started_at': datetime.now(timezone.utc).isoformat(),
               'previous_release': str(BASE), 'target_release': str(TARGET),
               'before': before, 'patches': manifest['patches']}
    (WORK / 'guard-before.json').write_text(json.dumps(receipt, indent=2))
    point(TARGET)
    try:
        ctl('start', SERVICE)
        if ctl('show', SERVICE, '-p', 'ExecMainStatus', '--value') != '0':
            raise RuntimeError('GUARD_SMOKE_FAILED')
        invocation = ctl('show', SERVICE, '-p', 'InvocationID', '--value')
        output = subprocess.check_output(['journalctl', '_SYSTEMD_INVOCATION_ID=' + invocation,
                                          '--no-pager', '-o', 'cat'], text=True)
        if 'OUTSIDE_MARKET_WINDOW' not in output:
            raise RuntimeError('GUARD_NOT_OBSERVED')
        if counters(config) != before or config_path.read_bytes() != config_bytes:
            raise RuntimeError('UNEXPECTED_SIDE_EFFECT')
        ctl('start', TIMER)
        receipt.update({'status': 'DEPLOYED', 'after': counters(config),
                        'timer_next': ctl('show', TIMER, '-p', 'NextElapseUSecRealtime', '--value'),
                        'guard_output': output.strip()})
        if receipt['after'] != before:
            raise RuntimeError('COUNTERS_CHANGED')
    except Exception:
        ctl('stop', TIMER)
        point(BASE)
        raise
    (WORK / 'guard-deployment.json').write_text(json.dumps(receipt, indent=2))
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
