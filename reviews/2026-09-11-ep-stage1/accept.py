"""SG stage-one acceptance against the single authoritative budget journal."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import resource
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
HOME = Path('/home/projects/quant')
WORK = HOME / 'data/ep/stage1-20260911'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['prepare', 'refresh', 'sources', 'collect', 'plan', 'run', 'inspect'])
    args = parser.parse_args()
    if not ROOT.is_relative_to(HOME):
        raise ValueError('SG_ONLY')
    from dotenv import load_dotenv
    from src.breakouts.ep.event_worker import WorkerConfig, budget
    from src.breakouts.ep.store import EpStore
    from src.utils.io import atomic_save_json
    load_dotenv('/etc/quant/market-data.env', override=False)
    load_dotenv('/etc/quant/ep-event-worker.env', override=False)
    path = WORK / 'worker.json'
    if args.command == 'prepare':
        if path.exists():
            raise ValueError('ACCEPTANCE_ALREADY_PREPARED')
        config = json.loads(Path('/etc/quant/ep-event-worker.json').read_text())
        config.update(output_directory=str(WORK / 'reports'), queue_database=str(WORK / 'pipeline.sqlite3'),
            enabled=True, collect_enabled=True, jobs=[], delivery_enabled=False,
            official_registry_path=str(ROOT / 'configs/ep_official_domains.json'),
            identity_catalog_path=str(HOME / 'data/catalog/quant.duckdb'),
            identity_snapshot_root=str(HOME / 'data/lake/security_master'),
            identity_source_root=str(HOME / 'outputs/data_audits/security_master_candidates'),
            analysis_protocol='event-context', commentary_style='personal', news_pages_per_feed=4,
            source_jobs_per_cycle=8)
        WorkerConfig.model_validate(config)
        atomic_save_json(config, path)
        path.chmod(0o600)
        print(json.dumps({'prepared': True, 'authoritative_budget_preserved': True, 'delivery': False}))
        return
    config = WorkerConfig.model_validate_json(path.read_text())
    if args.command == 'refresh':
        config = config.model_copy(update={'official_registry_path': str(ROOT / 'configs/ep_official_domains.json')})
        atomic_save_json(config.model_dump(), path)
        path.chmod(0o600)
        print(json.dumps({'registry_updated': True, 'delivery_enabled': config.delivery_enabled}))
        return
    if args.command == 'sources':
        import os
        from urllib.parse import urlsplit
        from src.breakouts.ep.official_sources import OfficialSourceRouter, load_official_registry
        from src.breakouts.ep.queue import PipelineQueue
        from src.breakouts.ep.models import CatalystSnapshot, NEW_YORK
        from src.data.public_articles import PublicArticleClient
        registry = load_official_registry(ROOT / 'configs/ep_official_domains.json')
        queue = PipelineQueue(config.queue_database, read_only=True)
        with queue.connection() as db:
            rows = [queue._row(row) for row in db.execute("SELECT * FROM jobs WHERE stage='SOURCE' ORDER BY created_at")]
        candidates = {}
        for row in rows:
            if row['ticker'] in registry['issuers'] and row['payload']['event']['event_type_hint'] == 'EARNINGS':
                candidates.setdefault(row['ticker'], row)
        hosts = {h for r in registry['issuers'].values() for h in (urlsplit(r['root_url']).hostname, r['ir_host'])}
        http = PublicArticleClient(allowed_hosts=hosts, max_requests=30, deadline_seconds=120, max_bytes=5_000_000)
        evidence = EpStore(WORK / 'ir-audit.sqlite3')
        original = EpStore(config.database, read_only=True)
        now = datetime.now(timezone.utc)
        profiles = original.profiles(now)
        resolver = OfficialSourceRouter(evidence, None, {'version': 'audit', 'issuers': {}}, http, registry)
        results = []
        for row in list(candidates.values())[:4]:
            event = row['payload']['event']
            day = event['published_at'][:10]
            run = evidence.start_run(day, day, {'source_queue_job_id': row['job_id'], 'ir_only_audit': True}, now)
            doc = CatalystSnapshot(event['document_id'], event['revision_id'], row['ticker'], 'ARTICLE',
                datetime.fromisoformat(event['published_at']).astimezone(NEW_YORK).date().isoformat(),
                event['published_at'], event['first_seen_at'], event['evidence'])
            evidence.save_page(run, {'origin': 'REAL_DISCOVERY_QUEUE', 'queue_job_id': row['job_id']}, [doc])
            result = resolver.resolve_event(run, {'ticker': row['ticker'], 'identity': profiles[row['ticker']]['profile']}, event)
            source = evidence.source_detail(result['source_id'])
            results.append({'ticker': row['ticker'], 'queue_job_id': row['job_id'], 'result': result,
                            'characters': (source.get('parsed') or {}).get('characters')})
        output = {'observed_at': now.isoformat(), 'rows': results, 'public_requests': http.requests,
                  'llm_requests': 0, 'discord_messages': 0}
        atomic_save_json(output, WORK / 'ir-audit.json')
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return
    if args.command in {'collect', 'plan', 'run'}:
        import importlib.util
        spec = importlib.util.spec_from_file_location('ep_worker_cli', ROOT / 'scripts/run_ep_event_worker.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.argv = ['worker', '--config', str(path), args.command]
        if args.command != 'plan':
            sys.argv.append('--execute')
        tick = time.monotonic()
        module.main()
        metrics = {'elapsed_seconds': round(time.monotonic() - tick, 3),
                   'peak_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
        atomic_save_json(metrics, WORK / (args.command + '-resources.json'))
        return
    from src.breakouts.ep.queue import PipelineQueue
    queue = PipelineQueue(config.queue_database, read_only=True)
    store = EpStore(config.database, read_only=True)
    with queue.connection() as db:
        jobs = [dict(row) for row in db.execute('SELECT ticker,stage,state,reason,created_at,updated_at,attempts,result FROM jobs ORDER BY created_at,stage')]
    output = {'observed_at': datetime.now(timezone.utc).isoformat(), 'budget': budget(store),
        'queue': queue.summary(), 'jobs': jobs, 'sources': []}
    for row in jobs:
        result = json.loads(row['result']) if row['result'] else {}
        if row['stage'] == 'SOURCE' and result.get('source_id'):
            source = store.source_detail(result['source_id'])
            output['sources'].append({'ticker': row['ticker'], 'status': row['reason'],
                'source_id': result['source_id'], 'url': source['result'].get('final_url'),
                'paragraphs': len((source.get('parsed') or {}).get('paragraphs', [])),
                'characters': (source.get('parsed') or {}).get('characters'),
                'route': source['result'].get('source_route')})
    atomic_save_json(output, WORK / 'inspection.json')
    print(json.dumps({k: v for k, v in output.items() if k != 'jobs'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
