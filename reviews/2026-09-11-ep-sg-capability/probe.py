#!/usr/bin/env python3
"""Isolated SG capability audit. No model, delivery, scheduler or production writes."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

CASES = {
    'AFRM': ('2026-08-27', 'Affirm reports fourth fiscal quarter 2026 results',
        'https://investors.affirm.com/news-releases/news-release-details/affirm-reports-fourth-fiscal-quarter-2026-results'),
    'ANF': ('2026-08-26', 'Abercrombie & Fitch Co. Reports Second Quarter Fiscal 2026 Results',
        'https://abercrombieandfitchcompany.gcs-web.com/news-releases/news-release-details/abercrombie-fitch-co-reports-second-quarter-fiscal-2026-results'),
    'GTLB': ('2026-09-01', 'GitLab Reports Second Quarter Fiscal Year 2027 Financial Results',
        'https://ir.gitlab.com/news/news-details/2026/GitLab-Reports-Second-Quarter-Fiscal-Year-2027-Financial-Results/default.aspx'),
    'NYAX': ('2026-08-25', 'Nayax Enters into Definitive Agreement to Acquire IPS Group, a Leading Smart Parking Technology Provider',
        'https://ir.nayax.com/news/news-details/2026/Nayax-Enters-into-Definitive-Agreement-to-Acquire-IPS-Group-a-Leading-Smart-Parking-Technology-Provider/default.aspx'),
    'PLAB': ('2026-08-26', 'Photronics Reports Third Quarter 2026 Results',
        'https://photronicsinc.gcs-web.com/news-releases/news-release-details/photronics-reports-third-quarter-2026-results'),
    'SAIC': ('2026-08-31', 'SAIC Announces Second Quarter of Fiscal Year 2027 Results',
        'https://investors.saic.com/news-releases/news-release-details/saic-announces-second-quarter-fiscal-year-2027-results'),
    'SNOW': ('2026-09-02', 'Snowflake Reports Financial Results for the Second Quarter of Fiscal 2027',
        'https://investors.snowflake.com/news/news-details/2026/Snowflake-Reports-Financial-Results-for-the-Second-Quarter-of-Fiscal-2027/default.aspx'),
}


def sources(output, production, *, sec_only=False, symbols=None, timeout=10):
    from src.breakouts.ep.identity import load_identity_snapshot
    from src.breakouts.ep.models import CatalystSnapshot, digest
    from src.breakouts.ep.official_sources import OfficialSourceRouter, load_official_registry
    from src.breakouts.ep.store import EpStore
    from src.data.public_articles import PublicArticleClient
    now = datetime.now(timezone.utc)
    identities = load_identity_snapshot(now, catalog_path=production / 'data/catalog/quant.duckdb',
        snapshot_root=production / 'data/lake/security_master',
        source_root=production / 'outputs/data_audits/security_master_candidates')
    registry = load_official_registry(ROOT / 'configs/ep_official_domains.json')
    hosts = {host for row in registry['issuers'].values()
             for host in (urlsplit(row['root_url']).hostname, row['ir_host'])}
    http = PublicArticleClient(allowed_hosts=hosts, max_requests=50, deadline_seconds=180,
                               timeout_seconds=timeout, max_bytes=5_000_000)
    store = EpStore(output / 'sources.sqlite3')
    days = [row[0] for symbol, row in CASES.items() if not symbols or symbol in symbols]
    run = store.start_run(min(days), max(days), {'audit_only': True}, now)
    if sec_only:
        import os
        from dotenv import load_dotenv
        from src.data.sec_attachments import SecDisclosureClient
        from src.breakouts.ep.discovery import OfficialSourceDiscovery
        load_dotenv('/etc/quant/ep-event-worker.env', override=False)
        http = SecDisclosureClient(contact_email=os.getenv('SEC_CONTACT_EMAIL'),
                                   max_requests=25, deadline_seconds=120, max_bytes=5_000_000)
        router = OfficialSourceDiscovery(store, http, registry)
    else:
        router = OfficialSourceRouter(store, None, {'version': 'audit', 'issuers': {}}, http, registry)
    results = []
    for symbol, (day, title, url) in CASES.items():
        if symbols and symbol not in symbols:
            continue
        profile = identities.profile(symbol, now)
        if not profile:
            results.append({'ticker': symbol, 'status': 'CURRENT_BULK_IDENTITY_UNAVAILABLE'})
            continue
        doc = CatalystSnapshot(digest(['audit', symbol, url]), digest([title, day]), symbol,
            'ARTICLE', day, day + 'T12:00:00+00:00', now.isoformat(),
            {'title': title, 'url': url, 'audit_seed': True, 'publication_time': 'NOT_VERIFIED'})
        store.save_page(run, {'audit_seed_not_news_discovery': True}, [doc])
        event = {'document_id': doc.document_id, 'revision_id': doc.revision_id,
                 'published_at': doc.published_at, 'evidence': doc.payload}
        started = time.monotonic()
        result = router.resolve_event(run, {'ticker': symbol, 'identity': profile}, event)
        source = store.source_detail(result['source_id'])
        parsed = source.get('parsed') or {}
        results.append({'ticker': symbol, 'identity': profile, 'result': result,
            'characters': parsed.get('characters'), 'paragraphs': len(parsed.get('paragraphs', [])),
            'attachments': [{'result': store.source_detail(a['source_id'])['result'],
                             'paragraphs': len((store.source_detail(a['source_id']).get('parsed') or {}).get('paragraphs', []))}
                            for a in result.get('attachments', [])],
            'elapsed_seconds': round(time.monotonic() - started, 3)})
        print(symbol, result['status'], flush=True)
    return {'identity_provenance': identities.provenance, 'identity_diagnostics': identities.diagnostics,
            'requests': http.requests, 'http_trace': http.trace, 'rows': results,
            'historical_availability': 'NOT_VERIFIED', 'sample': 'KNOWN_URLS_NOT_UNIVERSE_RECALL'}


def market(output, env_file):
    from dotenv import load_dotenv
    load_dotenv(env_file, override=False)
    from src.data import fmp
    from src.breakouts.ep.market_worker import MarketShadowStore
    from src.breakouts.ep.models import NEW_YORK
    store = MarketShadowStore(output / 'market.sqlite3')
    cases = [('GTLB', '2026-09-02'), ('VEEV', '2026-08-27'), ('NYAX', '2026-08-25')]
    names = [s for s, _ in cases]
    tasks = [('TRADE', None, None), ('QUOTE', None, None)]
    tasks += [(kind, symbol, day) for symbol, day in cases for kind in ('1MIN', '5MIN', 'EOD')]
    fields = {'symbol', 'price', 'tradeSize', 'size', 'timestamp', 'bidPrice', 'askPrice', 'bidSize',
              'askSize', 'volume', 'date', 'open', 'high', 'low', 'close', 'vwap', 'adjClose'}
    deadline, results, calls = time.monotonic() + 150, [], 0
    for kind, symbol, day in tasks:
        now = datetime.now(timezone.utc)
        if time.monotonic() >= deadline:
            results.append({'kind': kind, 'ticker': symbol, 'status': 'CAPACITY_DEFERRED'})
            continue
        started = time.monotonic()
        try:
            calls += 1
            timeout = min(10, deadline - started)
            if kind in {'TRADE', 'QUOTE'}:
                rows = fmp.get_ep_extended_batch(names, kind=kind.lower(), timeout=timeout)
            elif kind == '1MIN':
                rows = fmp.get_ep_minute_day(symbol, day, timeout=timeout)
            else:
                endpoint = '/historical-chart/5min' if kind == '5MIN' else '/historical-price-eod/full'
                rows = fmp._ep_records(endpoint, {'symbol': symbol, 'from': day, 'to': day}, timeout=timeout)
            if len(rows) > 1000 or any(not isinstance(r, dict) for r in rows):
                raise ValueError('BOUNDED_RECORDS_REQUIRED')
            if symbol and any(r.get('symbol') not in {None, symbol} for r in rows):
                raise ValueError('RESPONSE_SYMBOL_MISMATCH')
            clean = [{k: v for k, v in r.items() if k in fields and type(v) in {str, int, float, type(None)}} for r in rows]
            rid = store.save(symbol or 'BATCH', kind, {'records': clean, 'session_requested': day,
                'status': 'RAW_UNVERIFIED', 'raw_field_names': sorted({k for r in rows for k in r})}, now)
            result = {'kind': kind, 'ticker': symbol, 'day': day, 'rows': len(rows), 'receipt_id': rid,
                'raw_field_names': sorted({k for r in rows for k in r}), 'status': 'RAW_UNVERIFIED',
                'elapsed_seconds': round(time.monotonic() - started, 3)}
            if kind in {'1MIN', '5MIN'}:
                dates = sorted(str(r.get('date', '')) for r in rows)
                result.update(first=dates[0] if dates else None, last=dates[-1] if dates else None,
                    premarket_labels=sum('04:00' <= d[11:16] < '09:30' for d in dates),
                    off_day_rows=sum(not d.startswith(day) for d in dates),
                    fractional_volumes=sum(isinstance(r.get('volume'), (int, float)) and r['volume'] % 1 != 0 for r in rows),
                    summed_volume=sum(r.get('volume') or 0 for r in rows),
                    missing_opening_labels=[day + ' 09:' + str(m) + ':00' for m in range(30, 35)
                        if day + ' 09:' + str(m) + ':00' not in dates] if kind == '1MIN' else [])
            if kind in {'TRADE', 'QUOTE', 'EOD'}:
                result['sample'] = clean
            results.append(result)
            print(symbol or 'BATCH', kind, len(rows), flush=True)
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            results.append({'ticker': symbol, 'kind': kind, 'status': 'FETCH_FAILED',
                'error_type': type(exc).__name__, 'http_status': status})
            if status in {401, 403, 429}:
                break
    return {'requests': calls, 'observed_market_time': datetime.now(NEW_YORK).isoformat(), 'rows': results,
        'confirmation': 'BLOCKED_CONTRACT_UNVERIFIED', 'volume_scope': 'NOT_ASSUMED'}


def attachments(output, *, anf_script=False, anf_feed=False):
    import os
    from dotenv import load_dotenv
    from src.data.public_articles import PublicArticleClient
    from src.data.sec_attachments import SecDisclosureClient
    from src.breakouts.ep.store import EpStore
    from src.breakouts.ep.pdf_worker import parse_pdf_bounded
    load_dotenv('/etc/quant/ep-event-worker.env', override=False)
    public = PublicArticleClient(allowed_hosts={'corporate.abercrombie.com'},
        max_requests=4, deadline_seconds=40)
    sec = SecDisclosureClient(contact_email=os.getenv('SEC_CONTACT_EMAIL'),
        max_requests=4, deadline_seconds=40, max_bytes=5_000_000)
    targets = [('ANF', 'json', public, 'https://corporate.abercrombie.com/wp-json/anfco/v1/latest-releases'),
        ('PLAB', 'pdf', sec, 'https://www.sec.gov/Archives/edgar/data/810136/000081013626000006/plab8-K_3q-earn-exhibit99-2.pdf')]
    if anf_feed:
        targets = [('ANF', 'json', public, 'https://corporate.abercrombie.com/wp-json/anfco/v1/latest-releases?size=12&post=news&page=1&minDate=2026-08-26&maxDate=2026-08-26')]
    now = datetime.now(timezone.utc)
    store = EpStore(output / 'sources.sqlite3')
    run = store.start_run('2026-08-26', '2026-08-26', {'audit_only': True}, now)
    batch = store.start_source_run(run, {'observed_official_links_only': True}, now)
    rows = []
    if anf_script:
        import re
        url = 'https://corporate.abercrombie.com/wp-content/themes/anfco/blocks/build/press-listing/frontend/frontend.js?m=1788241774g'
        public._check_robots(url)
        response = public._get(url)
        result = {'status': 'FETCHED' if response.status == 200 else 'HTTP_FAILURE',
                  'received_at': datetime.now(timezone.utc).isoformat(), 'kind': 'PUBLIC_JS_INSPECTION_ONLY'}
        store.save_fetch(batch, url, result, datetime.now(timezone.utc), raw=response.body)
        text = response.body.decode('utf-8', errors='replace')
        rows = [{'url': url, 'http_status': response.status, 'bytes': len(response.body),
                 'callsite_snippets': [text[max(0,m.start()-700):m.end()+1200]
                    for m in list(re.finditer(r'latestReleases|pressRelease|URLSearchParams', text))[:12]]}]
        store.finish_source_run(batch, {'status': 'INSPECTION_ONLY'}, datetime.now(timezone.utc))
        return {'rows': rows, 'requests': public.requests, 'javascript_executed': False}
    for symbol, kind, client, url in targets:
        result = client.fetch_pdf(url) if kind == 'pdf' else client.fetch_json(url)
        body = result.pop(kind, None)
        result['kind'] = kind
        fid = store.save_fetch(batch, url, result, datetime.now(timezone.utc), raw=body)
        summary = {'ticker': symbol, 'url': url, 'fetch_id': fid, **result}
        if body and kind == 'json':
            value = json.loads(body)
            summary['json_sample'] = value[:2] if isinstance(value, list) else value
        if body and kind == 'pdf':
            parsed = parse_pdf_bounded(body)
            summary['parsed'] = {k: v for k, v in parsed.items() if k != 'paragraphs'}
            summary['paragraph_count'] = len(parsed.get('paragraphs', []))
            summary['paragraph_sample'] = parsed.get('paragraphs', [])[:8]
        rows.append(summary)
    store.finish_source_run(batch, {'status': 'AUDIT_COMPLETE_NOT_SOURCE_APPROVAL'}, datetime.now(timezone.utc))
    return {'rows': rows, 'requests': public.requests + sec.requests,
            'financial_facts_verified': False, 'historical_availability': 'NOT_VERIFIED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['sources', 'sec', 'market', 'attachments', 'anf-script', 'anf-feed'])
    parser.add_argument('--symbols', nargs='+', choices=sorted(CASES))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--production-root', type=Path, default=Path('/home/projects/quant'))
    parser.add_argument('--env-file', type=Path, default=Path('/etc/quant/market-data.env'))
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--timeout', type=int, choices=[10, 20], default=10)
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'mode': args.mode, 'public_request_cap': {'sources': 50, 'sec': 25, 'market': 11, 'attachments': 8, 'anf-script': 4, 'anf-feed': 4}[args.mode],
            'writes': False, 'llm_requests': 0, 'discord_messages': 0}))
        return
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'tmp'):
        parser.error('Output must be in this isolated checkout tmp directory')
    output.mkdir(parents=True, exist_ok=False)
    if args.mode in {'sources', 'sec'}:
        result = sources(output, args.production_root, sec_only=args.mode == 'sec', symbols=args.symbols, timeout=args.timeout)
    else:
        result = (attachments(output, anf_script=args.mode == 'anf-script', anf_feed=args.mode == 'anf-feed')
                  if args.mode in {'attachments', 'anf-script', 'anf-feed'} else market(output, args.env_file))
    from src.utils.io import atomic_save_json
    atomic_save_json({'observed_at': datetime.now(timezone.utc).isoformat(), **result,
        'llm_requests': 0, 'discord_messages': 0}, output / 'report.json')


if __name__ == '__main__':
    main()
