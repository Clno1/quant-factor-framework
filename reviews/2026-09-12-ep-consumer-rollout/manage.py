"""Off-market SG consumer cutover. Preserve credentials, ledger and outbox."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

HOME = Path('/home/projects/quant')
CONFIG = Path('/etc/quant/ep-event-worker.json')
LINK = HOME / 'ep-event-current'
OLD_TIMER = 'quant-ep-event-review.timer'
OLD_SERVICE = 'quant-ep-event-review-root.service'
UNITS = ['quant-ep-consumer@.service', 'quant-ep-consumers.slice',
         'quant-ep-price.timer', 'quant-ep-news.timer', 'quant-ep-source.timer']
TIMERS = [u for u in UNITS if u.endswith('.timer')]


def ctl(*args):
    return subprocess.check_output(['systemctl', *args], text=True).strip()


def snapshot(unit):
    raw = ctl('show', unit, '-p', 'ActiveState', '-p', 'UnitFileState')
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def backup_db(source, target):
    if source and Path(source).is_file():
        with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('BACKUP_INTEGRITY_FAILED')


def point_to(target):
    temporary = LINK.with_name('ep-consumer-next-' + os.urandom(4).hex())
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, LINK)


def restore(work, before):
    ctl('stop', OLD_TIMER)
    for timer in TIMERS:
        subprocess.run(['systemctl', 'disable', '--now', timer], check=False, capture_output=True)
    for lane in ('price', 'news', 'source'):
        subprocess.run(['systemctl', 'stop', 'quant-ep-consumer@' + lane + '.service'], check=False, capture_output=True)
    point_to(before['previous_release'])
    shutil.copy2(work / 'config-before.json', CONFIG)
    for unit in UNITS:
        saved, active = work / 'units' / unit, Path('/etc/systemd/system') / unit
        if saved.exists():
            shutil.copy2(saved, active)
        else:
            active.unlink(missing_ok=True)
    ctl('daemon-reload')
    for timer, state in before['timers'].items():
        if state['UnitFileState'] == 'enabled':
            ctl('enable', timer)
        if state['ActiveState'] == 'active':
            ctl('start', timer)
    # Never roll back the model ledger, delivered outbox or observations.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'deploy', 'status'])
    parser.add_argument('--release', type=Path, required=True)
    parser.add_argument('--expected-current', required=True)
    args = parser.parse_args()
    release = args.release.resolve()
    if os.geteuid() != 0 or not release.is_relative_to(HOME / 'releases'):
        raise ValueError('SG_ROOT_RELEASE_REQUIRED')
    manifest = json.loads((release / 'ep_release_manifest.json').read_text())
    for name, expected in manifest['files'].items():
        p = release / name
        if p.is_symlink() or not p.resolve().is_relative_to(release) or hashlib.sha256(p.read_bytes()).hexdigest() != expected:
            raise ValueError('RELEASE_MANIFEST_MISMATCH')
    sys.path.insert(0, str(release))
    from src.breakouts.ep.event_worker import WorkerConfig, budget, cycle
    from src.breakouts.ep.store import EpStore
    from src.breakouts.ep.queue import PipelineQueue
    from src.breakouts.ep.price_discovery import PriceStore
    from src.breakouts.ep.consumers import market_window
    from src.utils.file_lock import file_lock
    from src.utils.io import atomic_save_json
    raw = json.loads(CONFIG.read_text())
    before_config = WorkerConfig.model_validate(raw)
    if args.command == 'status':
        print(json.dumps({'release': str(LINK.resolve()), 'commit': manifest['commit'],
            'timers': {t: snapshot(t) for t in [OLD_TIMER] + TIMERS},
            'independent_consumers_enabled': before_config.independent_consumers_enabled,
            'price_discovery_enabled': before_config.price_discovery_enabled,
            'budget': budget(EpStore(before_config.database, read_only=True))}))
        return
    if str(LINK.resolve()) != args.expected_current:
        raise ValueError('CURRENT_RELEASE_CHANGED')
    work = HOME / 'data/ep' / ('consumer-rollout-' + release.name)
    changes = {'independent_consumers_enabled': True, 'price_discovery_enabled': True,
        'price_database': before_config.price_database or str(HOME / 'data/ep/price_discovery.sqlite3'),
        'price_batches_per_cycle': 5, 'price_deadline_seconds': 40,
        'price_news_jobs': 32, 'price_news_concurrency': 2, 'price_news_deadline_seconds': 40,
        'identity_jobs_per_cycle': 128, 'source_jobs_per_cycle': 8, 'source_deadline_seconds': 45}
    updated = WorkerConfig.model_validate({**raw, **changes})
    if args.command == 'plan':
        print(json.dumps({'status': 'PLAN_ONLY', 'commit': manifest['commit'], 'changes': changes,
            'backup': str(work), 'model_requests': 0, 'discord_messages': 0,
            'off_market': not market_window(datetime.now(timezone.utc))}))
        return
    if market_window(datetime.now(timezone.utc)):
        raise ValueError('OFF_MARKET_CUTOVER_REQUIRED')
    if any(snapshot(t)['ActiveState'] == 'active' for t in TIMERS):
        raise ValueError('NEW_CONSUMERS_ALREADY_ACTIVE')
    if work.exists():
        raise ValueError('BACKUP_DIRECTORY_ALREADY_EXISTS')
    before = {'previous_release': str(LINK.resolve()), 'timers': {t: snapshot(t) for t in [OLD_TIMER] + TIMERS},
              'created_at': datetime.now(timezone.utc).isoformat()}
    ctl('stop', OLD_TIMER)
    if ctl('show', OLD_SERVICE, '-p', 'ActiveState', '--value') not in {'inactive', 'failed'}:
        if before['timers'][OLD_TIMER]['ActiveState'] == 'active':
            ctl('start', OLD_TIMER)
        raise ValueError('WAIT_FOR_IN_FLIGHT_MODEL_WORKER')
    try:
        with ExitStack() as stack:
            stack.enter_context(file_lock(Path(before_config.database).with_suffix('.event-worker.lock')))
            stack.enter_context(file_lock(Path(before_config.queue_database).with_suffix('.ingest.lock')))
            work.mkdir(mode=0o700)
            (work / 'units').mkdir()
            shutil.copy2(CONFIG, work / 'config-before.json')
            (work / 'config-before.json').chmod(0o600)
            for unit in UNITS:
                path = Path('/etc/systemd/system') / unit
                if path.is_symlink():
                    raise ValueError('UNEXPECTED_EXISTING_UNIT_SYMLINK')
                if path.is_file():
                    shutil.copy2(path, work / 'units' / unit)
            before['budget'] = budget(EpStore(before_config.database, read_only=True))
            atomic_save_json(before, work / 'before.json')
            for name in ['database', 'queue_database', 'reviews_database', 'outbox_database', 'price_database']:
                backup_db(getattr(before_config, name), work / (name + '.sqlite3'))
            # Verify additive queue initialization and read-only analysis planning on a copy first.
            probe_queue = work / 'acceptance-queue.sqlite3'
            backup_db(before_config.queue_database, probe_queue)
            PipelineQueue(probe_queue)
            plan = cycle(updated.model_copy(update={'queue_database': str(probe_queue), 'enabled': False,
                'collect_enabled': False, 'delivery_enabled': False}))
            if plan['external_requests'] or plan['budget_before'] != plan['budget_after']:
                raise ValueError('PLAN_CHANGED_MODEL_BUDGET')
            atomic_save_json({'status': plan['status'], 'budget': plan['budget_after'],
                              'external_requests': 0}, work / 'preflight.json')
            PipelineQueue(updated.queue_database)
            PriceStore(updated.price_database)
            for unit in UNITS:
                shutil.copy2(release / 'deploy/systemd' / unit, Path('/etc/systemd/system') / unit)
            point_to(release)
            atomic_save_json({**raw, **changes}, CONFIG)
            CONFIG.chmod(0o600)
            preserved = json.loads(CONFIG.read_text())
            if any(preserved[k] != v for k, v in raw.items() if k not in changes):
                raise ValueError('UNRELATED_CONFIG_CHANGED')
            ctl('daemon-reload')
            for lane in ('price', 'news', 'source'):
                ctl('start', 'quant-ep-consumer@' + lane + '.service')
                if ctl('show', 'quant-ep-consumer@' + lane + '.service', '-p', 'ExecMainStatus', '--value') != '0':
                    raise ValueError('OFF_MARKET_SERVICE_SMOKE_FAILED')
            if budget(EpStore(updated.database, read_only=True)) != before['budget']:
                raise ValueError('MODEL_BUDGET_CHANGED_DURING_DEPLOY')
            for timer in TIMERS:
                ctl('enable', '--now', timer)
            if before['timers'][OLD_TIMER]['ActiveState'] == 'active':
                ctl('start', OLD_TIMER)
            atomic_save_json({'release': str(release), 'commit': manifest['commit'],
                'deployed_at': datetime.now(timezone.utc).isoformat(), 'changes': changes,
                'budget_unchanged': True, 'existing_delivery_config_preserved': True,
                'off_market_smoke_only': True, 'backup': str(work)}, work / 'deployment.json')
    except BaseException:
        if (work / 'before.json').exists():
            restore(work, before)
        elif before['timers'][OLD_TIMER]['ActiveState'] == 'active':
            ctl('start', OLD_TIMER)
        raise
    print((work / 'deployment.json').read_text())


if __name__ == '__main__':
    main()
