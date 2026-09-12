#!/usr/bin/env python3
"""One-shot isolated acceptance. Never calls a model or Discord."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import resource
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.breakouts.ep.event_worker import WorkerConfig
from src.breakouts.ep.event_ingest import registry_for_candidates
from src.breakouts.ep.identity import load_identity_snapshot
from src.breakouts.ep.price_discovery import PriceStore, discover
from src.breakouts.ep.price_news import collect_price_news
from src.breakouts.ep.provider import FmpEpProvider
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.store import EpStore
from src.breakouts.ep.pipeline import process_identities, process_sources
from src.breakouts.ep.latency import report
from src.breakouts.ep.models import NEW_YORK, timestamp
from src.utils.io import atomic_save_json


def write_diagnostic_config(output):
    config = WorkerConfig(database=str(output / 'evidence.sqlite3'),
        reviews_database=str(output / 'reviews-unused.sqlite3'), output_directory=str(output / 'reports-unused'),
        key_file=str(output / 'key-not-configured'), queue_database=str(output / 'queue.sqlite3'),
        price_database=str(output / 'prices.sqlite3'))
    atomic_save_json(config.model_dump(), output / 'diagnostics.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--batches', type=int, choices=range(1, 61), default=5)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--sources', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'status': 'PLAN_ONLY', 'max_price_requests': args.batches,
                          'max_news_requests': 4, 'max_source_jobs': 2 if args.sources else 0,
                          'llm_requests': 0, 'discord_messages': 0}))
        return
    original = WorkerConfig.model_validate_json(args.config.read_text())
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    replacements = {'database': str(output / 'evidence.sqlite3'), 'queue_database': str(output / 'queue.sqlite3'),
        'price_database': str(output / 'prices.sqlite3'), 'price_discovery_enabled': True,
        'price_batches_per_cycle': args.batches, 'price_deadline_seconds': 60, 'price_news_jobs': 2,
        'identity_fallback_requests': 0, 'source_jobs_per_cycle': 2}
    protected = {Path(p).resolve() for p in (original.database, original.queue_database, original.price_database) if p}
    if any(Path(replacements[k]).resolve() in protected for k in ('database', 'queue_database', 'price_database')):
        raise ValueError('ISOLATED_DATABASES_REQUIRED')
    config = original.model_copy(update=replacements)
    clock = lambda: datetime.now(timezone.utc)
    now = clock()
    queue, store = PipelineQueue(config.queue_database), EpStore(config.database)
    write_diagnostic_config(output)
    snapshot = load_identity_snapshot(now, catalog_path=config.identity_catalog_path or None,
        snapshot_root=config.identity_snapshot_root or None, source_root=config.identity_source_root or None)
    provider = FmpEpProvider()
    discovery = discover(queue, PriceStore(config.price_database), snapshot, provider, config, clock=clock)
    atomic_save_json(discovery, output / 'scan.json')
    news = collect_price_news(queue, store, provider, config, clock=clock)
    identities = process_identities(queue, store, provider, snapshot, config, clock=clock)
    sources = {'status': 'NOT_REQUESTED'}
    if args.sources:
        from src.data.sec_attachments import SecDisclosureClient
        from src.breakouts.ep.discovery import OfficialSourceDiscovery
        cache = json.loads((Path(original.output_directory) / 'sec_company_index.json').read_text())
        if not timedelta(0) <= now - datetime.fromisoformat(cache['received_at']) < timedelta(hours=24):
            raise ValueError('FRESH_SEC_INDEX_REQUIRED')
        registry, _ = registry_for_candidates(cache['payload'], snapshot.profiles)
        client = SecDisclosureClient(contact_email=os.getenv('SEC_CONTACT_EMAIL'), max_requests=16,
                                     deadline_seconds=75, max_bytes=5_000_000)
        resolver = OfficialSourceDiscovery(store, client, registry, max_documents=2,
                                            max_filings=2, max_exhibits=2, clock=clock)
        sources = {'counts': process_sources(queue, store, resolver, config, clock=clock),
                   'http_requests': client.requests, 'scope': 'SEC_ONLY_ACCEPTANCE'}
    with queue.connection() as db:
        symbols = [r[0] for r in db.execute("SELECT DISTINCT ticker FROM jobs WHERE stage='NEWS'")]
    session = now.astimezone(NEW_YORK).date().isoformat()
    timings = [report(queue, s, session, clock()) for s in symbols]
    atomic_save_json(timings, output / 'latency.json')
    atomic_save_json(timings, output / ('latency-' + discovery['scan_id'] + '.json'))
    result = {'started_at': timestamp(now), 'finished_at': timestamp(clock()), 'discovery': discovery,
              'news': news, 'identity': identities, 'sources': sources, 'queue': queue.summary(),
              'observed_candidates': symbols, 'llm_requests': 0, 'discord_messages': 0,
              'process_peak_rss_linux_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    atomic_save_json(result, output / 'summary.json')
    atomic_save_json(result, output / ('summary-' + discovery['scan_id'] + '.json'))
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
