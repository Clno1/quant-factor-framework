#!/usr/bin/env python3
"""Run one bounded EP consumer; no LLM calls or Discord delivery."""
import argparse
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.breakouts.ep.event_worker import WorkerConfig
from src.breakouts.ep.consumers import run_lane, consumer_status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--lane', choices=('price', 'news', 'source'), required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--status', action='store_true')
    args = parser.parse_args()
    if args.config.stat().st_size > 100_000:
        raise ValueError('WORKER_CONFIG_TOO_LARGE')
    config = WorkerConfig.model_validate_json(args.config.read_text())
    result = (consumer_status(config, args.lane, datetime.now(timezone.utc)) if args.status
              else run_lane(config, args.lane, execute=args.execute))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result.get('status') in {'FAILED', 'DEGRADED'} else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error_type': type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
