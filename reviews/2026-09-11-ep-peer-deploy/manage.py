"""SG-only EP release switch with queue backup; no model or Discord requests."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HOME = Path('/home/projects/quant')
CONFIG = Path('/etc/quant/ep-event-worker.json')
LINK = HOME / 'ep-event-current'
TIMER = 'quant-ep-event-review.timer'
SERVICE = 'quant-ep-event-review-root.service'


def systemctl(*args):
    return subprocess.check_output(['systemctl', *args], text=True).strip()


def backup_db(path, destination):
    if Path(path).is_file():
        with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst, pages=100)


def verify():
    if not ROOT.is_relative_to(HOME / 'releases'):
        raise ValueError('SG_RELEASE_REQUIRED')
    manifest = json.loads((ROOT / 'ep_release_manifest.json').read_text())
    for name, expected in manifest.items():
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('RELEASE_HASH_MISMATCH')
    return len(manifest)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'accept', 'deploy', 'status'])
    parser.add_argument('--expected-current', required=True)
    args = parser.parse_args()
    count = verify()
    from src.breakouts.ep.event_worker import WorkerConfig, budget, cycle
    from src.breakouts.ep.queue import PipelineQueue
    from src.breakouts.ep.store import EpStore
    from src.utils.io import atomic_save_json
    from src.utils.file_lock import file_lock
    work = HOME / 'data/ep' / ('peer-deploy-' + ROOT.name)
    raw = json.loads(CONFIG.read_text())
    config = WorkerConfig.model_validate(raw)
    journal = budget(EpStore(config.database, read_only=True))
    if args.command == 'status':
        with sqlite3.connect(Path(config.queue_database).as_uri() + '?mode=ro', uri=True) as db:
            schema = db.execute('SELECT version FROM ep_pipeline_schema').fetchone()[0]
        print(json.dumps({'files_verified': count, 'release': str(LINK.resolve()),
            'timer': systemctl('show', TIMER, '-p', 'ActiveState', '--value'),
            'delivery_enabled': config.delivery_enabled, 'channel_id': config.expected_channel_id,
            'queue_schema': schema, 'budget': journal,
            'watch_input_configured': bool(config.watch_input_path), 'financial_input_configured': bool(config.financial_input_path)}))
        return
    if str(LINK.resolve()) != args.expected_current:
        raise ValueError('ACTIVE_RELEASE_CHANGED')
    if args.command == 'prepare':
        systemctl('stop', TIMER)
        if systemctl('show', SERVICE, '-p', 'ActiveState', '--value') not in {'inactive', 'failed'}:
            raise ValueError('WAIT_FOR_CURRENT_WORKER_DO_NOT_INTERRUPT_PAID_CALL')
        with file_lock(Path(config.database).with_suffix('.event-worker.lock')):
            work.mkdir(mode=0o700, exist_ok=False)
            shutil.copy2(CONFIG, work / 'worker-before.json')
            (work / 'worker-before.json').chmod(0o600)
            atomic_save_json({'previous_release': args.expected_current, 'budget': journal,
                'prepared_at': datetime.now(timezone.utc).isoformat()}, work / 'before.json')
            backup_db(config.queue_database, work / 'queue-before.sqlite3')
            backup_db(config.outbox_database, work / 'outbox-before.sqlite3')
            backup_db(config.database, work / 'ledger-before.sqlite3')
            raw['delivery_enabled'] = False
            WorkerConfig.model_validate(raw)
            atomic_save_json(raw, CONFIG)
            CONFIG.chmod(0o600)
        print(json.dumps({'prepared': True, 'backup': str(work), 'old_channel_delivery': 'PAUSED', 'timer': 'STOPPED'}))
        return
    if not (work / 'before.json').is_file():
        raise ValueError('PREPARE_REQUIRED')
    if args.command == 'accept':
        queue_copy = work / 'queue-acceptance.sqlite3'
        if queue_copy.exists():
            raise ValueError('ACCEPTANCE_ALREADY_EXISTS')
        backup_db(work / 'queue-before.sqlite3', queue_copy)
        start = time.monotonic()
        now = datetime.now(timezone.utc)
        queue = PipelineQueue(queue_copy)
        before = len(queue.ready('SOURCE', now, limit=5000))
        migrated = queue.consolidate_sources(now)
        after = len(queue.ready('SOURCE', now, limit=5000))
        scoped = {s: {'event_groups': len(queue.explain(s)['events']),
                      'asof_jobs': len(queue.timeline(s, datetime(2026, 9, 11, 12, 15, tzinfo=timezone.utc))['jobs'])}
                  for s in ['ORCL', 'CPRT', 'RH']}
        cfg = config.model_copy(update={'queue_database': str(queue_copy), 'enabled': False,
            'collect_enabled': False, 'delivery_enabled': False, 'jobs': []})
        plan = cycle(cfg)
        if plan['external_requests'] or plan['budget_before'] != plan['budget_after']:
            raise ValueError('READ_ONLY_PREFLIGHT_CHANGED_BUDGET')
        result = {'source_jobs_before': before, 'article_jobs_grouped': migrated,
            'event_jobs_after': after, 'scope': scoped, 'plan_status': plan['status'],
            'model_requests': 0, 'discord_messages': 0, 'elapsed_seconds': round(time.monotonic() - start, 3),
            'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            'coverage': 'REAL_QUEUE_STRUCTURAL_REPLAY_NOT_LIVE_RECALL', 'budget': journal}
        atomic_save_json(result, work / 'acceptance.json')
        print(json.dumps(result))
        return
    acceptance = json.loads((work / 'acceptance.json').read_text())
    if acceptance['model_requests'] or acceptance['discord_messages']:
        raise ValueError('ISOLATED_ACCEPTANCE_REQUIRED')
    if systemctl('show', SERVICE, '-p', 'ActiveState', '--value') not in {'inactive', 'failed'}:
        raise ValueError('WORKER_MUST_BE_IDLE')
    if config.delivery_enabled:
        raise ValueError('CHANNEL_MIGRATION_MUST_STAY_PAUSED')
    # There have been no workers since prepare. If that changes, take fresh
    # backups and re-accept rather than silently restoring a stale queue.
    before = json.loads((work / 'before.json').read_text())
    if journal != before['budget']:
        raise ValueError('BUDGET_CHANGED_DURING_ACCEPTANCE')
    with file_lock(Path(config.database).with_suffix('.event-worker.lock')):
        if systemctl('show', TIMER, '-p', 'ActiveState', '--value') != 'inactive':
            raise ValueError('TIMER_MUST_REMAIN_STOPPED')
        next_link = LINK.with_name('ep-peer-next-' + os.urandom(4).hex())
        try:
            PipelineQueue(config.queue_database)
            next_link.symlink_to(ROOT, target_is_directory=True)
            os.replace(next_link, LINK)
            atomic_save_json({'release': str(ROOT), 'deployed_at': datetime.now(timezone.utc).isoformat(),
                              'delivery_enabled': False, 'budget_preserved': True}, work / 'deployment.json')
        except BaseException:
            next_link.unlink(missing_ok=True)
            # No worker has started: restore only the queue, never budget or outbox.
            backup_db(work / 'queue-before.sqlite3', Path(config.queue_database))
            next_link.symlink_to(args.expected_current, target_is_directory=True)
            os.replace(next_link, LINK)
            raise
    systemctl('start', TIMER)
    print(json.dumps({'deployed': True, 'release': str(ROOT), 'timer': 'ACTIVE',
                      'delivery_enabled': False, 'next': 'CONFIGURE_NEW_CHANNEL', 'backup': str(work)}))


if __name__ == '__main__':
    main()
