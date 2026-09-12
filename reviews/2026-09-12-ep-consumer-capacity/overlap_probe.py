"""Bounded overlap acceptance in this review's isolated SG database copy."""
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path('/home/projects/quant/tmp/ep-consumer-capacity-20260912-live')
config = json.loads((root / 'config.json').read_text())
assert all(Path(config[k]).resolve().parent == root for k in ('database', 'queue_database', 'price_database'))
assert not config['delivery_enabled'] and not config['webhook_file']
assert config['key_file'] == str(root / 'unused.key')
config.update(price_discovery_enabled=True, price_batches_per_cycle=5, price_deadline_seconds=40)
path = root / 'overlap-config.json'
path.write_text(json.dumps(config))
children, handles = {}, []
started = time.monotonic()
peak_rss, peak_pss = 0, 0


def memory(pid):
    try:
        rows = Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines()
        values = {line.split(':', 1)[0]: int(line.split()[1]) for line in rows if line.startswith(('Rss:', 'Pss:'))}
        return values.get('Rss', 0), values.get('Pss', 0)
    except (FileNotFoundError, ProcessLookupError):
        return 0, 0


try:
    for lane in ('price', 'news', 'source'):
        handle = (root / f'overlap-{lane}.log').open('w')
        handles.append(handle)
        children[lane] = subprocess.Popen([sys.executable, 'scripts/run_ep_consumer.py', '--config', str(path),
            '--lane', lane, '--execute'], stdout=handle, stderr=handle)
    while any(child.poll() is None for child in children.values()):
        sizes = [memory(child.pid) for child in children.values()]
        peak_rss = max(peak_rss, sum(r[0] for r in sizes))
        peak_pss = max(peak_pss, sum(r[1] for r in sizes))
        if time.monotonic() - started > 150:
            raise TimeoutError('ISOLATED_CONSUMER_OVERLAP_TIMEOUT')
        time.sleep(.2)
finally:
    for child in children.values():
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
    for handle in handles:
        handle.close()

result = {'scope': 'THREE_REAL_CONSUMERS_ISOLATED_COPY_PRICE_SCOPE_500_SYMBOLS',
    'elapsed_seconds': round(time.monotonic() - started, 3),
    'sampled_sum_peak_rss_kib': peak_rss, 'sampled_sum_peak_pss_kib': peak_pss,
    'sampling_interval_seconds': .2, 'excludes_legacy_model_and_web_processes': True,
    'llm_requests': 0, 'discord_messages': 0, 'lanes': {}}
for lane, child in children.items():
    lines = (root / f'overlap-{lane}.log').read_text().splitlines()
    summaries = []
    for line in lines:
        try:
            summaries.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    result['lanes'][lane] = {'exit_code': child.returncode, 'result': summaries[-1] if summaries else None}
(root / 'overlap-summary.json').write_text(json.dumps(result, ensure_ascii=False))
print(json.dumps(result, ensure_ascii=False))
