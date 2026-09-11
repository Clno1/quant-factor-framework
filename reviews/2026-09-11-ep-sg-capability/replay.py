#!/usr/bin/env python3
"""Replay SG receipts through current diagnostics and build model packets offline."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from probe import CASES
from src.breakouts.ep.financial_context import select_context_paragraphs
from src.breakouts.ep.llm_event_context import prepare_context_packet, validate_context
from src.breakouts.ep.market_quality import inspect_market_records
from src.breakouts.ep.models import digest
from src.breakouts.ep.official_sources import OfficialSourceRouter, load_official_registry
from src.breakouts.ep.store import EpStore
from src.utils.io import atomic_save_json


def replay_sources(evidence):
    fetched = {}
    for folder in ('sources-first', 'anf-current'):
        path = evidence / folder / 'sources.sqlite3'
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
            for url, raw, payload in db.execute('SELECT url,raw_body,payload_json FROM ep_source_fetches'):
                fetched[url] = (raw, json.loads(payload))

    class Recorded:
        def validate_url(self, url):
            return url
        def fetch_same_host(self, url, *, kind='html'):
            raw, result = fetched.get(url, (None, {'status': 'NOT_IN_RECORDED_SCOPE',
                'final_url': url, 'received_at': datetime.now(timezone.utc).isoformat()}))
            return {**result, **({kind: raw} if raw is not None else {})}

    class Receipts:
        def cached_fetch(self, *args):
            return None
        def save_fetch(self, batch, url, result, now, raw=None):
            return 'offline-' + digest([url, result.get('raw_sha256')])

    registry = load_official_registry(ROOT / 'configs/ep_official_domains.json')
    router = OfficialSourceRouter(Receipts(), None, {'version': 'audit', 'issuers': {}}, Recorded(), registry)
    report = json.loads((evidence / 'sources-first/report.json').read_text())
    rows = []
    for row in report['rows']:
        symbol = row['ticker']
        _, title, url = CASES[symbol]
        result, _, _ = router._resolve_ir('offline', {'ticker': symbol, 'identity': row['identity']},
                                         {'evidence': {'title': title, 'url': url}})
        rows.append({'ticker': symbol, 'status': result['status'],
                     'blocking_reasons': result.get('blocking_reasons', [])})
    return rows


def packets(evidence):
    store = EpStore(evidence / 'sec-first/sources.sqlite3', read_only=True)
    report = json.loads((evidence / 'sec-first/report.json').read_text())
    rows = []
    for row in report['rows']:
        if row['result']['status'] != 'DOCUMENT_MATCHED':
            rows.append({'ticker': row['ticker'], 'status': row['result']['status']})
            continue
        source = store.source_detail(row['result']['source_id'])
        ids = select_context_paragraphs(source)
        packet = prepare_context_packet(source, ids)
        req = packet['request']
        selected = [d['disclosure_id'] for d in req['financial_context']['disclosures'][:4]]
        validation = validate_context(packet, {**{k: req[k] for k in ('request_id', 'document_id', 'text_revision')},
            'scope_status': 'UNCERTAIN', 'disclosure_ids': selected, 'notes': []})
        rows.append({'ticker': row['ticker'], 'status': 'OFFLINE_PACKET_AND_DISCLOSURE_VALIDATION_PASSED',
            'selected_paragraphs': len(ids), 'disclosure_count': len(req['financial_context']['disclosures']),
            'materialized_disclosures': len(validation['sections']['source_disclosures']),
            'comparison_blockers': validation['sections']['comparison_blockers'],
            'ai_interpretations': 0, 'packet_hash': packet['packet_hash']})
    return rows


def market(evidence):
    path = evidence / 'market-first/market.sqlite3'
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as db:
        receipts = [(symbol, kind, datetime.fromisoformat(at), json.loads(raw))
                    for symbol, kind, at, raw in db.execute('SELECT ticker,kind,observed_at,payload_json FROM ep_market_receipts')]
    rows, by_symbol = [], {}
    for symbol, kind, at, payload in receipts:
        if kind in {'TRADE', 'QUOTE'}:
            for record in payload['records']:
                rows.append({'ticker': record['symbol'], 'kind': kind,
                    **inspect_market_records('EXTENDED_' + kind, [record], session='2026-09-11', as_of=at)})
        elif kind == '1MIN':
            rows.append({'ticker': symbol, 'kind': kind, **inspect_market_records('MINUTE_DAY', payload['records'],
                         session=payload['session_requested'], as_of=at)})
        if symbol != 'BATCH':
            by_symbol.setdefault(symbol, {})[kind] = payload['records']
    comparisons = []
    for symbol, records in by_symbol.items():
        minutes = {r['date']: r for r in records['1MIN']}
        complete, volume_equal, prices_equal = 0, 0, 0
        for five in records['5MIN']:
            start = datetime.strptime(five['date'], '%Y-%m-%d %H:%M:%S')
            group = [minutes.get((start + timedelta(minutes=i)).strftime('%Y-%m-%d %H:%M:%S')) for i in range(5)]
            if any(r is None for r in group):
                continue
            complete += 1
            volume_equal += abs(sum(r['volume'] for r in group) - five['volume']) < 1e-6
            aggregate = {'open': group[0]['open'], 'high': max(r['high'] for r in group),
                         'low': min(r['low'] for r in group), 'close': group[-1]['close']}
            prices_equal += all(abs(value - five[key]) < 1e-6 for key, value in aggregate.items())
        one, five = (sum(r['volume'] for r in records[k]) for k in ('1MIN', '5MIN'))
        daily = records['EOD'][0]['volume']
        comparisons.append({'ticker': symbol, 'minute_volume': one, 'five_minute_volume': five,
            'eod_volume': daily, 'minute_to_daily_ratio': one / daily,
            'complete_five_minute_buckets': complete, 'equal_volume_buckets': volume_equal,
            'equal_ohlc_buckets': prices_equal, 'comparability_verified': False,
            'reason': 'CROSS_INTERVAL_SCOPE_OR_REVISION_NOT_ESTABLISHED'})
    return {'diagnostics': rows, 'cross_interval_comparisons': comparisons}


def network_summary(evidence):
    reports = []
    for name in ('sources-first', 'sec-first', 'market-first', 'anf-current',
                 'attachments-first', 'anf-script', 'anf-feed'):
        path = evidence / name / 'report.json'
        if not path.exists():
            path = evidence / (name + '-report.json')
        raw = path.read_bytes()
        value = json.loads(raw)
        reports.append({'name': name, 'observed_at': value['observed_at'],
            'requests': value['requests'], 'report_sha256': hashlib.sha256(raw).hexdigest(),
            'rows': [{'ticker': row.get('ticker'), 'kind': row.get('kind'),
                      'status_at_capture': row.get('status', row.get('result', {}).get('status')),
                      'pdf_status': row.get('parsed', {}).get('status'),
                      'pdf_pages': row.get('parsed', {}).get('page_count')}
                     for row in value['rows']]})
    return {'probes': reports, 'fmp_requests': sum(r['requests'] for r in reports if r['name'] == 'market-first'),
            'public_source_requests': sum(r['requests'] for r in reports if r['name'] != 'market-first'),
            'model_requests': 0, 'discord_messages': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = {'mode': 'OFFLINE_REPLAY_NO_MODEL_OUTPUT', 'external_requests': 0, 'discord_messages': 0,
              'sg_network_acceptance': network_summary(args.evidence),
              'ir_diagnostics': replay_sources(args.evidence), 'sec_financial_packets': packets(args.evidence),
              'market': market(args.evidence)}
    atomic_save_json(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
