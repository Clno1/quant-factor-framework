#!/usr/bin/env python3
"""Credential-free IR access probe; never approves sources or calls an AI model."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.official_sources import load_official_registry, links
from src.data.public_articles import PublicArticleClient
from src.utils.io import atomic_save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=ROOT / 'configs/ep_official_domains.json')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--tickers', nargs='+', help='Restrict the bounded probe to registered issuers')
    parser.add_argument('--archive', action='store_true', help='Retain fetched public HTML beside the report')
    args = parser.parse_args()
    registry = load_official_registry(args.registry)
    if args.tickers:
        registry['issuers'] = {s: registry['issuers'][s] for s in args.tickers}
    if not args.execute:
        print('Plan only: at most 30 credential-free HTTP requests; no LLM, no Discord, no writes.')
        return
    hosts = {h for row in registry['issuers'].values() for h in (urlsplit(row['root_url']).hostname, row['ir_host'])}
    client = PublicArticleClient(allowed_hosts=hosts, max_requests=30, deadline_seconds=90)
    rows = []
    for symbol, row in registry['issuers'].items():
        for role, url in [('CORPORATE_ROOT', row['root_url'])] + [('IR_INDEX', u) for u in row['index_urls']]:
            result = client.fetch_same_host(url)
            raw = result.pop('html', None)
            found = links(raw, result['final_url']) if raw else []
            if raw and args.archive:
                directory = args.output.parent / 'raw'
                directory.mkdir(parents=True, exist_ok=True)
                (directory / (result['raw_sha256'] + '.html')).write_bytes(raw)
            rows.append({'ticker': symbol, 'role': role, 'url': url, **result,
                'ir_link_present': any(urlsplit(l['url']).netloc == row['ir_host'] for l in found),
                'link_count': len(found), 'links': found if args.archive else []})
    report = {'observed_at': datetime.now(timezone.utc).isoformat(), 'requests': client.requests,
              'rows': rows, 'identity_verification': 'NOT_PERFORMED_CAPABILITY_PROBE_ONLY',
              'llm_requests': 0, 'discord_messages': 0}
    atomic_save_json(report, args.output)
    for row in rows:
        print(row['ticker'], row['role'], row['status'], 'IR link:', row['ir_link_present'])


if __name__ == '__main__':
    main()
