#!/usr/bin/env python3
"""Refresh only rotation's cap observations; never publish prices or send."""
from pathlib import Path
import sys
import argparse
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None):
    from src.alerts.config import load_local_env
    from src.group_analytics.rotation.capitalization import refresh_observation
    from src.group_analytics.settings import load_group_analytics_settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--output-root', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.env_file and load_local_env(args.env_file) is None:
            raise ValueError('Missing environment file')
        if not load_group_analytics_settings().enabled:
            raise ValueError('Group analytics disabled')
        data = refresh_observation(root=args.output_root)
        print(json.dumps({k: data[k] for k in ('source_session', 'captured_at', 'mapped', 'eligible', 'coverage')}))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error_code': 'CAP_OBSERVATION_FAILED', 'error_type': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
