#!/usr/bin/env python3
"""Read-only EP stage acceptance at explicit historical cutoffs. No market/model requests."""
import argparse
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.breakouts.ep.event_worker import WorkerConfig
from src.breakouts.ep.models import NEW_YORK, ticker, timestamp
from src.breakouts.ep.pipeline import queue_path
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.session_audit import report, consumer_coverage
from src.utils.io import atomic_save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--as-of', help='Timezone-aware cutoff; defaults to now')
    parser.add_argument('--tickers', default='', help='Optional diagnostic symbols, not ground truth')
    parser.add_argument('--checkpoints', default='08:15,09:25,09:35', help='New York times; future checkpoints remain WAITING')
    parser.add_argument('--references', type=Path, help='JSON list: ticker, observed_at, reference (external unverified benchmark)')
    parser.add_argument('--output', type=Path, help='Optional explicit JSON report path; input databases remain read-only')
    args = parser.parse_args()
    if args.config.stat().st_size > 100_000:
        raise ValueError('WORKER_CONFIG_TOO_LARGE')
    config = WorkerConfig.model_validate_json(args.config.read_text())
    captured = datetime.now(timezone.utc)
    cutoff = datetime.fromisoformat(args.as_of) if args.as_of else captured
    timestamp(cutoff)
    if cutoff > captured:
        raise ValueError('FUTURE_ASOF_NOT_ALLOWED')
    queue = PipelineQueue(queue_path(config), read_only=True)
    symbols = [ticker(s.strip()) for s in args.tickers.split(',') if s.strip()]
    references = []
    if args.references:
        if args.references.stat().st_size > 100_000:
            raise ValueError('REFERENCE_FILE_TOO_LARGE')
        references = json.loads(args.references.read_text())
        if not isinstance(references, list) or len(references) > 200:
            raise ValueError('BOUNDED_REFERENCE_LIST_REQUIRED')
        for ref in references:
            ref['ticker'] = ticker(ref['ticker'])
            at = datetime.fromisoformat(ref['observed_at'])
            timestamp(at)
            if at.astimezone(NEW_YORK).date().isoformat() != args.session:
                raise ValueError('REFERENCE_SESSION_MISMATCH')
            symbols.append(ref['ticker'])
    cache = {}

    def at_time(at):
        key = timestamp(at)
        if key not in cache:
            cache[key] = report(queue, config.price_database, args.session, at, symbols=symbols, captured_at=captured)
            cache[key]['consumers'] = consumer_coverage(config.output_directory, args.session, at)
        return cache[key]

    current = at_time(cutoff)
    checkpoints = []
    values = args.checkpoints.split(',') if args.checkpoints else []
    if len(values) > 24:
        raise ValueError('TOO_MANY_CHECKPOINTS')
    for value in values:
        local_time = time.fromisoformat(value.strip())
        if local_time.tzinfo is not None:
            raise ValueError('CHECKPOINTS_USE_NEW_YORK_TIME')
        at = datetime.combine(date.fromisoformat(args.session), local_time, NEW_YORK)
        checkpoints.append(at_time(at) if at <= cutoff else {'as_of': timestamp(at), 'status': 'WAITING_FOR_CHECKPOINT'})
    comparisons = []
    for ref in references:
        at = datetime.fromisoformat(ref['observed_at'])
        value = {'ticker': ref['ticker'], 'observed_at': timestamp(at), 'reference': str(ref.get('reference', ''))[:500],
                 'reference_verified': False}
        if at > cutoff:
            value['status'] = 'WAITING_FOR_REFERENCE_TIME'
        else:
            item = next((r for r in at_time(at)['symbols'] if r['ticker'] == ref['ticker']), None)
            if item is None:
                value['status'] = 'REFERENCE_OUTSIDE_CAPTURE_WINDOW'
            else:
                value.update(status='PRICE_WATCH_OBSERVED_BY_REFERENCE' if item['price_discovered_at'] else 'REFERENCE_NOT_OBSERVED_AS_PRICE_WATCH',
                    first_watch_at=item['price_discovered_at'], reasons=item['reasons'], events=item['events'])
        comparisons.append(value)
    result = {'current': current, 'checkpoints': checkpoints, 'reference_comparisons': comparisons,
              'external_requests': 0, 'llm_requests': 0, 'discord_messages': 0}
    if args.output:
        protected = [Path(p).resolve() for p in (queue_path(config), config.price_database, config.database,
                     config.outbox_database, config.reviews_database, str(args.config), config.key_file) if p]
        output = args.output.resolve()
        if output in protected or output.suffix != '.json':
            raise ValueError('SEPARATE_JSON_REPORT_PATH_REQUIRED')
        atomic_save_json(result, output)
        print(json.dumps({'status': current['status'], 'report': str(output),
                          'symbol_count': len(current['symbols']), 'external_requests': 0}))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error_type': type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
