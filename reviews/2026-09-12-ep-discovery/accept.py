"""Read production queues, resolve selected originals in a new evidence database only."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.pipeline import select_source_jobs, source_plan
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.store import EpStore
from src.breakouts.ep.models import CatalystSnapshot
from src.breakouts.ep.official_sources import OfficialSourceRouter, load_official_registry
from src.breakouts.ep.event_ingest import registry_for_candidates
from src.breakouts.ep.watch import source_priority
from src.data.public_articles import PublicArticleClient
from src.data.sec_attachments import SecDisclosureClient
from src.utils.io import atomic_save_json


def prior_selection(queue, now, limit):
    ready = queue.ready('SOURCE', now, limit=5000)
    priorities = {s: source_priority(queue, s, now) for s in {r['ticker'] for r in ready}}
    ready.sort(key=lambda r: (-priorities[r['ticker']], r['created_at'], r['job_id']))
    hints = {'EARNINGS', 'GUIDANCE', 'M_AND_A', 'COMMERCIAL_CONTRACT', 'DEAL_TERMINATION'}
    preferred = [r for r in ready if r['payload']['event'].get('event_type_hint') in hints]
    general = [r for r in ready if r not in preferred]
    selected = [r for r in ready if r['attempts'] > 0][:max(1, limit // 4)]
    ids = {r['job_id'] for r in selected}
    selected += [r for r in preferred if r['job_id'] not in ids][:max(0, limit - max(1, limit // 4) - len(selected))]
    ids = {r['job_id'] for r in selected}
    selected += [r for r in general if r['job_id'] not in ids][:limit - len(selected)]
    ids = {r['job_id'] for r in selected}
    return selected + [r for r in preferred if r['job_id'] not in ids][:limit - len(selected)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--queue-clock', required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--known-case', choices=['ORCL', 'CPRT', 'RH'])
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a new directory; evidence must not be overwritten')
    config = json.loads(args.config.read_text())
    at = datetime.fromisoformat(args.queue_clock)
    queue = PipelineQueue(config['queue_database'], read_only=True)
    plan = source_plan(queue, at, 8)
    selected = select_source_jobs(queue, at, 8)
    before = prior_selection(queue, at, 8)
    result = {'scope': 'CURRENT_QUEUE_ROUTING_REGRESSION_NOT_HISTORICAL_DETECTION_PROOF',
              'started_at': datetime.now(timezone.utc).isoformat(), 'plan': plan,
              'before': [{'ticker': r['ticker'], 'title': r['payload']['event']['evidence']['title']} for r in before],
              'known_case': args.known_case, 'sources': [], 'llm_requests': 0,
              'discord_messages': 0, 'production_writes': 0, 'live_http_requests': 0}
    if args.known_case:
        selected = [r for r in queue.ready('SOURCE', at, limit=5000) if r['ticker'] == args.known_case][:1]
        result['scope'] = 'KNOWN_CASE_SOURCE_DIAGNOSIS_NOT_DISCOVERY_RECALL'
    args.output.mkdir(parents=True)
    def save():
        atomic_save_json(result, args.output / 'report.json')
    save()
    if not args.execute:
        print(json.dumps(result, indent=2))
        return
    # Only this new store receives receipts. Production evidence and queue are read-only.
    store = EpStore(args.output / 'sources.sqlite3')
    production = EpStore(config['database'], read_only=True)
    profiles = production.profiles(at)
    cache = json.loads((Path(config['output_directory']) / 'sec_company_index.json').read_text())
    registry, _ = registry_for_candidates(cache['payload'], [r['ticker'] for r in selected])
    official = load_official_registry(ROOT / 'configs/ep_official_domains.json')
    hosts = {h for row in official['issuers'].values() for h in (urlsplit(row['root_url']).hostname, row['ir_host'])}
    public = PublicArticleClient(allowed_hosts=hosts, max_requests=30, deadline_seconds=180, max_bytes=5_000_000)
    sec = SecDisclosureClient(contact_email=os.getenv('SEC_CONTACT_EMAIL'), max_requests=30,
                              deadline_seconds=180, max_bytes=5_000_000)
    clock = lambda: datetime.now(timezone.utc)
    resolver = OfficialSourceRouter(store, sec, registry, public, official,
                                    max_documents=8, max_filings=2, max_exhibits=2, clock=clock)
    deadline = time.monotonic() + 180
    for job in selected:
        if time.monotonic() >= deadline:
            result['stopped'] = 'AUDIT_DEADLINE'
            break
        symbol, event = job['ticker'], job['payload']['event']
        record = profiles.get(symbol)
        if not record or record['status'] != 'OK':
            result['sources'].append({'ticker': symbol, 'status': 'IDENTITY_UNAVAILABLE_AT_QUEUE_CLOCK'})
            save()
            continue
        with production.connection() as db:
            doc = db.execute('SELECT * FROM ep_documents WHERE document_id=? AND revision_id=?',
                             (event['document_id'], event['revision_id'])).fetchone()
        run = store.start_run(doc['event_date'], doc['event_date'], {}, clock())
        snapshot = CatalystSnapshot(doc['document_id'], doc['revision_id'], symbol, doc['kind'],
                                    doc['event_date'], doc['published_at'], doc['first_seen_at'], json.loads(doc['payload_json']))
        store.save_page(run, {}, [snapshot])
        resolved = resolver.resolve_event(run, {'ticker': symbol, 'identity': record['profile']}, event)
        result['sources'].append({'ticker': symbol, 'queue_job_id': job['job_id'],
                                  'title': event['evidence']['title'], 'result': resolved})
        result['live_http_requests'] = public.requests + sec.requests
        save()
        print(symbol, resolved['status'], flush=True)
    result['finished_at'] = clock().isoformat()
    result['counts'] = dict(Counter(s.get('result', s)['status'] for s in result['sources']))
    save()


if __name__ == '__main__':
    main()
