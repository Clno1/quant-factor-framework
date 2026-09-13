"""Scoped, preimage-checked release; never changes Git refs or data pointers."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def main():
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['pack', 'inspect', 'install'])
    p.add_argument('--root', type=Path, required=True)
    p.add_argument('--bundle', type=Path, required=True)
    a = p.parse_args()
    root, bundle = a.root.resolve(), a.bundle.resolve()
    if a.mode == 'pack':
        tested = json.loads((root / 'reviews/2026-09-13-security-isolation/evidence/tested-source-sha256.json').read_text())
        for name, expected in tested.items():
            assert sha(root / name) == expected, name
        files = {}
        for name in tested:
            result = subprocess.run(['git', 'show', 'HEAD:' + name], cwd=root, capture_output=True)
            files[name] = {'after': tested[name], 'before': hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None}
        bundle.mkdir(parents=True, exist_ok=False)
        manifest = {'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
                    'source_kind': 'reviewed_uncommitted_scoped_overlay', 'files': files}
        (bundle / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        with tarfile.open(bundle / 'source.tar.gz', 'w:gz', format=tarfile.USTAR_FORMAT) as archive:
            for name in files:
                archive.add(root / name, arcname=name, recursive=False)
        shutil.copy2(__file__, bundle / 'release.py')
        print(json.dumps(manifest, indent=2))
        return
    manifest = json.loads((bundle / 'manifest.json').read_text())
    actual = {name: sha(root / name) for name in manifest['files']}
    mismatches = {n: {'actual': actual[n], **v} for n, v in manifest['files'].items() if actual[n] not in (v['before'], v['after'])}
    print(json.dumps({'mismatches': mismatches, 'actual': actual}, indent=2), flush=True)
    assert not mismatches, 'unexpected production preimages'
    if a.mode == 'inspect':
        (bundle / 'preflight.json').write_text(json.dumps(actual, indent=2))
        return
    assert actual == json.loads((bundle / 'preflight.json').read_text()), 'source changed since preflight'
    sys_path = str(root)
    import sys
    sys.path.insert(0, sys_path)
    from src.breakouts.live.settings import IntradayMonitorSettings
    from src.utils.env import load_local_env
    load_local_env('/etc/quant/intraday-momentum-monitor.env')
    assert IntradayMonitorSettings.load().cup_handle_delivery_enabled is False
    backup = bundle / 'backup'
    backup.mkdir(exist_ok=False)
    lock = root / 'data/lake/staging/us_equity_coverage_incremental/.writer.lock'
    with lock.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        conn = sqlite3.connect((root / 'outputs/intraday_momentum_monitor/state.sqlite3').as_uri() + '?mode=ro', uri=True)
        tables = ['cup_handle_cycles', 'cup_handle_evaluations', 'cup_handle_session_observations', 'cup_handle_data_gaps']
        hashes = {n: hashlib.sha256(json.dumps(conn.execute('SELECT * FROM ' + n + ' ORDER BY rowid').fetchall()).encode()).hexdigest() for n in tables}
        conn.close()
        (bundle / 'live-tables-before.json').write_text(json.dumps(hashes, indent=2))
        with tarfile.open(bundle / 'source.tar.gz') as archive:
            assert set(archive.getnames()) == set(manifest['files'])
            for member in archive.getmembers():
                assert member.isfile() and not Path(member.name).is_absolute() and '..' not in Path(member.name).parts
                raw = archive.extractfile(member).read()
                assert hashlib.sha256(raw).hexdigest() == manifest['files'][member.name]['after']
            changed = []
            try:
                for name, values in manifest['files'].items():
                    dest = root / name
                    assert sha(dest) == actual[name], name
                    if actual[name] == values['after']:
                        continue
                    saved = backup / name
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        shutil.copy2(dest, saved)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    temp = dest.with_name(dest.name + '.release-tmp')
                    temp.write_bytes(archive.extractfile(name).read())
                    os.chmod(temp, dest.stat().st_mode & 0o777 if dest.exists() else 0o644)
                    os.replace(temp, dest)
                    changed.append(name)
                assert all(sha(root / n) == v['after'] for n, v in manifest['files'].items())
            except BaseException:
                for name in reversed(changed):
                    saved = backup / name
                    if saved.exists():
                        shutil.copy2(saved, root / name)
                    else:
                        (root / name).unlink()
                raise
        (bundle / 'installed.json').write_text(json.dumps({'changed': changed, 'files_verified': len(actual), 'delivery_enabled': False}, indent=2))
        print('INSTALLED', len(changed), 'changed files;', len(actual), 'verified')


if __name__ == '__main__':
    main()
