"""Compare bounded staging with the exact certified sources; publish nothing."""
import json
from pathlib import Path
import sys
import time

import duckdb
import pandas as pd

from scripts.update_us_equity_coverage import _stage_replacement_history


def main():
    report_path, output = map(Path, sys.argv[1:])
    output.mkdir(parents=True, exist_ok=False)
    report = json.loads(report_path.read_text())
    assert report['complete_scope'] and report['completed'] == 5295
    proofs = report['validated']
    paths = []
    for proof in proofs:
        manifest = Path(proof['manifest_path'])
        paths.append(manifest.parent / json.loads(manifest.read_text())['artifact'])
    started = time.monotonic()
    staged = _stage_replacement_history(paths, proofs, output)
    staged_seconds = time.monotonic() - started
    with duckdb.connect(staged['path'], read_only=True) as conn:
        conn.execute("SET threads=1")
        conn.execute("SET memory_limit='128MB'")
        actual = conn.execute("SELECT * FROM replacements WHERE date BETWEEN '2019-01-01' AND '2019-01-31'").fetchdf()
    monthly_seconds = time.monotonic() - started - staged_seconds
    pieces = []
    with duckdb.connect() as conn:
        conn.execute("SET threads=1")
        conn.execute("SET memory_limit='128MB'")
        for offset in range(0, len(paths), 25):
            pieces.append(conn.execute("SELECT * FROM read_parquet(?,hive_partitioning=false) WHERE date BETWEEN '2019-01-01' AND '2019-01-31'",
                                      [[str(p) for p in paths[offset:offset + 25]]]).fetchdf())
    expected = pd.concat(pieces, ignore_index=True)
    pd.testing.assert_frame_equal(actual.sort_values(['date', 'security_id']).reset_index(drop=True),
                                  expected.sort_values(['date', 'security_id']).reset_index(drop=True), check_exact=True)
    result = {'staging': staged, 'staging_seconds': staged_seconds,
              'first_month_query_seconds': monthly_seconds, 'first_month_rows': len(actual),
              'exact_all_columns_match': True, 'elapsed_seconds': time.monotonic() - started,
              'publication_attempted': False, 'source_requests': 0}
    (output / 'report.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
