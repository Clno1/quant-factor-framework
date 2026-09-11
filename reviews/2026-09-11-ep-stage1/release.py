"""Switch only the EP release/config/units, preserving other SG workloads and ledgers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HOME = Path('/home/projects/quant')
CONFIG = Path('/etc/quant/ep-event-worker.json')
LINK = HOME / 'ep-event-current'
BACKUP = HOME / 'data/ep/stage1-20260911/rollback'
UNITS = ['quant-ep-event-review-root.service', 'quant-ep-event-review.timer']


def systemctl(*args):
    return subprocess.check_output(['systemctl', *args], text=True).strip()


def verify():
    if not ROOT.is_relative_to(HOME / 'releases'):
        raise ValueError('SG_RELEASE_DIRECTORY_REQUIRED')
    manifest = json.loads((ROOT / 'ep_release_manifest.json').read_text())
    for name, expected in manifest.items():
        file = ROOT / name
        if not file.resolve().is_relative_to(ROOT) or file.is_symlink() or hashlib.sha256(file.read_bytes()).hexdigest() != expected:
            raise ValueError('RELEASE_MANIFEST_MISMATCH')
    from src.breakouts.ep.event_worker import WorkerConfig, budget
    from src.breakouts.ep.store import EpStore
    cfg = WorkerConfig.model_validate_json(CONFIG.read_text())
    result = budget(EpStore(cfg.database, read_only=True))
    return cfg, result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['verify', 'deploy', 'rollback'])
    parser.add_argument('--expected-current')
    args = parser.parse_args()
    cfg, budget = verify()
    if args.command == 'verify':
        print(json.dumps({'files_verified': True, 'budget': budget, 'current': str(LINK.resolve())}))
        return
    if args.command == 'deploy' and str(LINK.resolve()) != args.expected_current:
        raise ValueError('ACTIVE_RELEASE_CHANGED')
    if args.command == 'deploy' and budget['reserved_microusd'] >= budget['total_limit_microusd']:
        raise ValueError('TRIAL_BUDGET_EXHAUSTED')
    old_timer = systemctl('show', UNITS[1], '-p', 'ActiveState', '--value')
    backup_created = False
    systemctl('stop', UNITS[1])
    try:
        if systemctl('show', UNITS[0], '-p', 'ActiveState', '--value') not in {'inactive', 'failed'}:
            raise ValueError('EP_WORKER_RUNNING_WAIT_BEFORE_SWITCH')
        if args.command == 'deploy':
            BACKUP.mkdir(mode=0o700, exist_ok=False)
            shutil.copy2(CONFIG, BACKUP / 'worker.json')
            for name in UNITS:
                shutil.copy2(Path('/etc/systemd/system') / name, BACKUP / name)
            (BACKUP / 'previous-release.txt').write_text(str(LINK.resolve()))
            backup_created = True
            for name, path in [('ledger', cfg.database), ('outbox', cfg.outbox_database)]:
                if Path(path).exists():
                    with sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True) as source:
                        with sqlite3.connect(BACKUP / (name + '.sqlite3')) as dest:
                            source.backup(dest)
            from src.utils.io import atomic_save_json
            updated = cfg.model_dump()
            updated.update(analysis_protocol='event-context', commentary_style='personal', jobs=[],
                queue_database=str(HOME / 'data/ep/stage1-20260911/pipeline.sqlite3'),
                official_registry_path=str(LINK / 'configs/ep_official_domains.json'),
                identity_catalog_path=str(HOME / 'data/catalog/quant.duckdb'),
                identity_snapshot_root=str(HOME / 'data/lake/security_master'),
                identity_source_root=str(HOME / 'outputs/data_audits/security_master_candidates'),
                collect_enabled=True, enabled=True, delivery_enabled=True, allow_unreviewed_ai=True)
            from src.breakouts.ep.event_worker import WorkerConfig
            WorkerConfig.model_validate(updated)
            atomic_save_json(updated, CONFIG)
            CONFIG.chmod(0o600)
            for name in UNITS:
                shutil.copy2(ROOT / 'deploy/systemd' / name, Path('/etc/systemd/system') / name)
            target = ROOT
        else:
            # Never restore ledger/outbox snapshots: doing so could repeat paid calls or messages.
            shutil.copy2(BACKUP / 'worker.json', CONFIG)
            for name in UNITS:
                shutil.copy2(BACKUP / name, Path('/etc/systemd/system') / name)
            target = Path((BACKUP / 'previous-release.txt').read_text())
        temporary = LINK.with_name('ep-stage1-next')
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, LINK)
        systemctl('daemon-reload')
        systemctl('enable', '--now', UNITS[1])
        print(json.dumps({'release': str(target), 'budget_preserved': True, 'timer': 'ACTIVE',
                          'command': args.command, 'backup': str(BACKUP)}))
    except BaseException:
        if args.command == 'deploy' and backup_created:
            shutil.copy2(BACKUP / 'worker.json', CONFIG)
            for name in UNITS:
                shutil.copy2(BACKUP / name, Path('/etc/systemd/system') / name)
            restore = LINK.with_name('ep-stage1-restore')
            restore.symlink_to(Path((BACKUP / 'previous-release.txt').read_text()), target_is_directory=True)
            os.replace(restore, LINK)
            systemctl('daemon-reload')
        if old_timer == 'active':
            systemctl('start', UNITS[1])
        raise


if __name__ == '__main__':
    main()
