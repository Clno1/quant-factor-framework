"""Package committed dependencies plus EP-only working changes, never local secrets."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
OVERLAYS = ['src/breakouts/ep', 'tests/test_ep_*.py', 'src/alerts/ep_event.py',
    'src/data/fmp.py', 'src/data/public_articles.py', 'scripts/run_ep_event_worker.py',
    'scripts/configure_ep_event_discord.py', 'tests/fixtures/ep_peer_sources_20260911.json',
    'scripts/probe_ep_official_sources.py', 'scripts/run_ep_market_shadow.py',
    'configs/ep_official_domains.json', 'requirements-ep.txt',
    'deploy/systemd/ep-event-worker.example.json', 'deploy/systemd/quant-ep-event-review-root.service',
    'deploy/systemd/quant-ep-event-review.timer', 'reviews/2026-09-11-ep-stage1',
    'reviews/2026-09-11-ep-sg-capability', 'reviews/2026-09-11-ep-peer-deploy']


def main():
    output = Path(sys.argv[1])
    raw = subprocess.check_output(['git', 'archive', 'HEAD', 'src', 'tests', 'configs', 'scripts',
                                  'deploy/systemd', 'reviews'], cwd=ROOT)
    contents = {}
    allowed = {'.py', '.json', '.yaml', '.toml', '.service', '.timer', '.txt'}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            if member.isfile() and (Path(member.name).suffix in allowed or member.name.endswith('.env.example')):
                contents[member.name] = archive.extractfile(member).read()
    for pattern in OVERLAYS:
        for path in ROOT.glob(pattern):
            for file in path.rglob('*') if path.is_dir() else [path]:
                if file.is_file() and not file.is_symlink() and file.suffix in allowed and '__pycache__' not in file.parts:
                    contents[file.relative_to(ROOT).as_posix()] = file.read_bytes()
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())}
    encoded = json.dumps(manifest, sort_keys=True).encode()
    contents['ep_release_manifest.json'] = encoded
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'x:gz') as archive:
        for name, data in contents.items():
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o644
            archive.addfile(member, io.BytesIO(data))
    print(json.dumps({'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
        'release_id': hashlib.sha256(encoded).hexdigest()[:16], 'files': len(manifest)}))


if __name__ == '__main__':
    main()
