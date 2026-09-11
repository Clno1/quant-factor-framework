#!/usr/bin/env python3
"""Summarize archived follow-up receipts; no keys, HTTP, models or delivery."""
import argparse
import hashlib
import json
from pathlib import Path


def replay(evidence):
    names = ['afrm-recheck', 'plab-auto-pdf', 'plab-auto-pdf-5mb', 'saic-ir', 'snow-ir',
             'sec-expanded', 'volume-before-0400', 'volume-after-0400']
    result = {'scope': 'SEEDED_CAPABILITY_TEST_NOT_UNIVERSE_RECALL', 'receipts': [],
              'public_requests': 0, 'fmp_requests': 0, 'llm_requests': 0, 'discord_messages': 0,
              'contract_verified': False, 'missing_receipts': []}
    snapshots = {}
    for name in names:
        path = evidence / name / 'report.json'
        if not path.exists():
            result['missing_receipts'].append(name)
            continue
        raw = path.read_bytes()
        report = json.loads(raw)
        assert report['llm_requests'] == report['discord_messages'] == 0
        market = name.startswith('volume-')
        result['fmp_requests' if market else 'public_requests'] += report['requests']
        entry = {'name': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'requests': report['requests']}
        if market:
            snapshots[name] = report
            entry.update(at=report['observed_market_time'], comparisons=report['comparisons'],
                minute_results=[r for r in report['rows'] if r['kind'] == 'CURRENT_MINUTE'])
        else:
            entry['sources'] = [{'ticker': row['ticker'], 'status': row['result']['status'],
                'url': row['result'].get('final_url'), 'characters': row['characters'],
                'paragraphs': row['paragraphs'], 'attachments': [
                    {'status': a['result']['status'], 'paragraphs': a['paragraphs'],
                     'url': a['result'].get('final_url')} for a in row.get('attachments', [])]}
                for row in report['rows']]
        result['receipts'].append(entry)
    if len(snapshots) == 2:
        before, after = (snapshots[k] for k in ('volume-before-0400', 'volume-after-0400'))
        def quoted(report, kind):
            return {r['symbol']: r for x in report['rows'] if x['kind'] == kind for r in x.get('sample', [])}
        old, new = quoted(before, 'EXTENDED_QUOTE'), quoted(after, 'EXTENDED_QUOTE')
        result['cross_snapshot_observations'] = [{
            'ticker': symbol, 'before_volume': old[symbol].get('volume'), 'after_volume': new[symbol].get('volume'),
            'before_timestamp': old[symbol].get('timestamp'), 'after_timestamp': new[symbol].get('timestamp'),
            'volume_decreased': new[symbol]['volume'] < old[symbol]['volume']
                if type(old[symbol].get('volume')) in {int, float} and type(new[symbol].get('volume')) in {int, float} else None,
            'semantics': 'RESET_CANDIDATE_ONLY_NO_SCOPE_APPROVAL'} for symbol in sorted(old.keys() & new.keys())]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.evidence)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in {'receipts', 'cross_snapshot_observations'}}))


if __name__ == '__main__':
    main()
