#!/usr/bin/env python3
"""Explicit EP market capture or offline replay, without timers, LLMs or Discord."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.market_worker import MarketShadowStore, collect_market, replay_market
from src.breakouts.ep.models import ticker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--symbols', nargs='+')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--queue-database', type=Path)
    args = parser.parse_args()
    if args.queue_database and (not args.queue_database.is_absolute() or args.queue_database.resolve() == args.database.resolve()):
        parser.error('The pipeline and market databases must be distinct absolute paths')
    if not args.database.is_absolute() or bool(args.input) == bool(args.symbols):
        parser.error('Use an absolute dedicated database and either --symbols or --input')
    if args.input:
        if args.input.stat().st_size > 15_000_000:
            raise ValueError('MARKET_REPLAY_TOO_LARGE')
        bundle = json.loads(args.input.read_text())
        result = replay_market(bundle)
        if args.execute:
            MarketShadowStore(args.database).save(bundle['current']['ticker'], 'SHADOW_REPORT', result,
                                                  datetime.now(timezone.utc))
    else:
        symbols = list(dict.fromkeys(ticker(s) for s in args.symbols))
        if not 1 <= len(symbols) <= 5:
            parser.error('At most five symbols per bounded capture')
        result = (collect_market(MarketShadowStore(args.database), symbols) if args.execute else
                  {'mode': 'PLAN', 'symbols': symbols, 'max_http_requests': len(symbols) + 2,
                   'external_requests': 0, 'database_written': False, 'delivery': 'DISABLED_SHADOW_ONLY'})
    if args.execute and args.queue_database:
        from src.breakouts.ep.queue import PipelineQueue
        queue = PipelineQueue(args.queue_database)
        names = [bundle['current']['ticker']] if args.input else symbols
        for name in names:
            state = (result if args.input else {'status': 'RAW_UNVERIFIED',
                'outcomes': [o for o in result['outcomes'] if o.get('ticker') in {name, None}],
                'confirmation_blockers': result['confirmation_blockers']})
            observed = datetime.now(timezone.utc)
            queue.save_checkpoint('market:' + name, {**state, 'observed_at': observed.isoformat()}, observed)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
