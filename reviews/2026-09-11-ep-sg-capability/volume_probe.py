#!/usr/bin/env python3
"""One-shot cross-endpoint volume audit; never normalizes or enables trading."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def collect(output, *, provider, clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
    from src.breakouts.ep.market_session import expected_source_session
    from src.breakouts.ep.market_worker import MarketShadowStore
    from src.breakouts.ep.models import NEW_YORK
    symbols = ['AAPL', 'GTLB', 'NYAX']
    now = clock()
    session = now.astimezone(NEW_YORK).date().isoformat()
    previous = expected_source_session(session)
    store = MarketShadowStore(output / 'volume.sqlite3')
    tasks = [('EXTENDED_TRADE', '/batch-aftermarket-trade', {'symbols': ','.join(symbols)}),
             ('EXTENDED_QUOTE', '/batch-aftermarket-quote', {'symbols': ','.join(symbols)}),
             ('REGULAR_QUOTE', '/batch-quote', {'symbols': ','.join(symbols)})]
    for symbol in symbols:
        tasks.extend([('CURRENT_MINUTE', '/historical-chart/1min', {'symbol': symbol, 'from': session, 'to': session}),
                      ('PREVIOUS_EOD', '/historical-price-eod/full', {'symbol': symbol, 'from': previous, 'to': previous})])
    fields = {'symbol', 'date', 'timestamp', 'price', 'previousClose', 'open', 'high', 'low', 'close',
              'volume', 'tradeSize', 'bidPrice', 'askPrice', 'bidSize', 'askSize', 'vwap'}
    results, rows_by_kind, calls, stop = [], {}, 0, None
    end = monotonic() + 120
    for kind, endpoint, params in tasks:
        remaining = end - monotonic()
        if stop or remaining <= 0:
            results.append({'kind': kind, 'ticker': params.get('symbol'), 'status': stop or 'CAPACITY_DEFERRED'})
            continue
        started = clock()
        try:
            calls += 1
            rows = provider._ep_records(endpoint, params, timeout=min(10, remaining))
            if len(rows) > 1200 or any(not isinstance(r, dict) for r in rows):
                raise ValueError('BOUNDED_RECORDS_REQUIRED')
            if any(r.get('symbol', params.get('symbol')) not in symbols for r in rows):
                raise ValueError('RESPONSE_SYMBOL_MISMATCH')
            if 'symbol' in params and any(r.get('symbol', params['symbol']) != params['symbol'] for r in rows):
                raise ValueError('RESPONSE_SYMBOL_MISMATCH')
            rows = [{k: v for k, v in r.items() if k in fields and type(v) in {str, int, float, type(None)}
                     and (not isinstance(v, str) or len(v) <= 100)} for r in rows]
            received = clock()
            rid = store.save(params.get('symbol', 'BATCH'), kind, {'records': rows, 'endpoint': endpoint,
                'parameters': params, 'requested_at': started.isoformat(), 'contract_verified': False}, received)
            result = {'kind': kind, 'ticker': params.get('symbol'), 'receipt_id': rid, 'rows': len(rows),
                      'status': 'RAW_UNVERIFIED' if rows else 'NO_RECORDS', 'received_at': received.isoformat()}
            if kind == 'CURRENT_MINUTE':
                dates = sorted(str(r.get('date', '')) for r in rows)
                result.update(first=dates[0] if dates else None, last=dates[-1] if dates else None,
                    premarket_labels=sum(d.startswith(session) and '04:00' <= d[11:16] < '09:30' for d in dates),
                    off_day_rows=sum(not d.startswith(session) for d in dates))
            else:
                result['sample'] = rows
            for symbol in ([params['symbol']] if 'symbol' in params else symbols):
                rows_by_kind[(symbol, kind)] = rows if 'symbol' in params else [r for r in rows if r.get('symbol') == symbol]
            results.append(result)
        except Exception as exc:
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            results.append({'kind': kind, 'ticker': params.get('symbol'), 'status': 'FETCH_FAILED',
                            'error_type': type(exc).__name__, 'http_status': code if type(code) is int else None})
            if code in {401, 403, 429}:
                stop = 'PROVIDER_ACCESS_STOPPED'
    comparisons = []
    for symbol in symbols:
        values = {}
        for kind in ('EXTENDED_QUOTE', 'REGULAR_QUOTE', 'PREVIOUS_EOD'):
            rows = rows_by_kind.get((symbol, kind), [])
            values[kind] = rows[0].get('volume') if len(rows) == 1 else None
        ext, regular, eod = (values[k] for k in ('EXTENDED_QUOTE', 'REGULAR_QUOTE', 'PREVIOUS_EOD'))
        comparisons.append({'ticker': symbol, 'volumes': values,
            'extended_equals_regular': ext == regular if ext is not None and regular is not None else None,
            'extended_equals_previous_eod': ext == eod if ext is not None and eod is not None else None,
            'scope_inference': 'OBSERVED_VALUES_ONLY_NOT_PROVIDER_CONTRACT'})
    return {'started_at': now.isoformat(), 'observed_market_time': now.astimezone(NEW_YORK).isoformat(),
        'session': session, 'previous_session': previous, 'requests': calls, 'rows': results,
        'comparisons': comparisons, 'contract_verified': False, 'llm_requests': 0, 'discord_messages': 0,
        'limitations': ['NOT_A_COMPLETE_TRADE_FEED', 'VOLUME_RESET_AND_VENUE_SCOPE_UNVERIFIED',
                        'DO_NOT_SUM_TRADE_SIZE_OR_INFER_DOLLAR_TURNOVER']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'http_request_cap': 9, 'writes': False, 'llm_requests': 0, 'discord_messages': 0}))
        return
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / 'tmp'):
        parser.error('Output must be in this isolated checkout tmp directory')
    output.mkdir(parents=True, exist_ok=False)
    from dotenv import load_dotenv
    load_dotenv('/etc/quant/market-data.env', override=False)
    from src.data import fmp
    report = collect(output, provider=fmp)
    (output / 'report.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
