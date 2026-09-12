#!/usr/bin/env python3
"""Explicit, bounded consumer acceptance against a copy, never production queues."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import resource
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.breakouts.ep.consumers import run_lane, backlog
from src.breakouts.ep.event_worker import WorkerConfig
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.models import timestamp
from src.utils.io import atomic_save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--seed-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rounds', type=int, choices=(1, 2, 3), default=3)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--sources', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'status': 'PLAN_ONLY', 'max_news_http_requests': args.rounds * 64,
                          'max_source_http_requests': 63 if args.sources else 0,
                          'llm_requests': 0, 'discord_messages': 0}))
        return
    original = WorkerConfig.model_validate_json(args.config.read_text())
    output, seed = args.output.resolve(), args.seed_directory.resolve()
    if output.exists() or output == seed or output in seed.parents or seed in output.parents:
        raise ValueError('NEW_SEPARATE_ACCEPTANCE_DIRECTORY_REQUIRED')
    replacements = {'database': str(output / 'evidence.sqlite3'), 'queue_database': str(output / 'queue.sqlite3'),
        'price_database': str(output / 'prices.sqlite3'), 'reviews_database': str(output / 'reviews-unused.sqlite3'),
        'output_directory': str(output / 'reports'), 'key_file': str(output / 'unused.key'),
        'outbox_database': '', 'webhook_file': '', 'expected_channel_id': '', 'jobs': [],
        'enabled': True, 'collect_enabled': True, 'independent_consumers_enabled': True,
        'delivery_enabled': False, 'allow_unreviewed_ai': False, 'price_discovery_enabled': False,
        'price_news_jobs': 32, 'price_news_concurrency': 2, 'price_news_deadline_seconds': 40,
        'identity_fallback_requests': 0, 'source_jobs_per_cycle': 8, 'source_deadline_seconds': 45,
        'official_registry_path': str(Path(__file__).resolve().parents[1] / 'configs/ep_official_domains.json')}
    config = WorkerConfig.model_validate({**original.model_dump(), **replacements})
    output.mkdir(parents=True, mode=0o700)
    for name in ('queue.sqlite3', 'evidence.sqlite3', 'prices.sqlite3'):
        with sqlite3.connect((seed / name).as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(output / name) as target:
                source.backup(target)
    cache = Path(original.output_directory) / 'sec_company_index.json'
    if cache.is_file() and cache.stat().st_size < 3_000_000:
        atomic_save_json(json.loads(cache.read_text()), output / 'reports/sec_company_index.json')
    atomic_save_json(config.model_dump(), output / 'config.json')
    clock = lambda: datetime.now(timezone.utc)
    queue = PipelineQueue(config.queue_database)
    result = {'started_at': timestamp(clock()), 'scope': 'COPY_OF_PRIOR_REAL_QUEUE_ORIGINAL_TIMESTAMPS_RETAINED',
              'before': backlog(queue, clock()), 'news_cycles': [], 'llm_requests': 0, 'discord_messages': 0}
    for _ in range(args.rounds):
        cycle = run_lane(config, 'news', execute=True)
        result['news_cycles'].append(cycle)
        atomic_save_json(result, output / 'summary.json')
        if cycle['status'] != 'COMPLETED' or cycle.get('work', {}).get('status') == 'PROVIDER_COOLDOWN':
            break
    if args.sources:
        result['source_cycle'] = run_lane(config, 'source', execute=True)
    jobs = [job for cycle in result['news_cycles'] for job in cycle.get('work', {}).get('jobs', [])]
    result.update(finished_at=timestamp(clock()), after=backlog(queue, clock()),
        news_http_requests=sum(c.get('work', {}).get('http_requests', 0) for c in result['news_cycles']),
        fully_checked_jobs=sum(c.get('work', {}).get('fully_checked_jobs', 0) for c in result['news_cycles']),
        first_pass_jobs=sum(c.get('work', {}).get('first_pass_jobs', 0) for c in result['news_cycles']),
        outcomes=dict(Counter(j['reason'] for j in jobs)),
        process_peak_rss_linux_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    atomic_save_json(result, output / 'summary.json')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
