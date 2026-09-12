"""Export public-source receipts from the isolated discovery acceptance database."""
import argparse
import base64
import gzip
import json
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    cases = []
    for attempt in db.execute('SELECT * FROM ep_source_attempts ORDER BY observed_at'):
        result = json.loads(attempt['payload_json'])
        doc = db.execute('SELECT * FROM ep_documents WHERE document_id=? AND revision_id=?',
                         (attempt['document_id'], attempt['revision_id'])).fetchone()
        fetches = []
        for step in result.get('discovery_steps', []):
            row = db.execute('SELECT * FROM ep_source_fetches WHERE fetch_id=?', (step['fetch_id'],)).fetchone()
            meta = json.loads(row['payload_json'])
            raw = row['raw_body']
            if raw is None and meta.get('body_fetch_id'):
                raw = db.execute('SELECT raw_body FROM ep_source_fetches WHERE fetch_id=?', (meta['body_fetch_id'],)).fetchone()[0]
            fetches.append({'url': row['url'], 'result': meta,
                            'gzip_base64': base64.b64encode(gzip.compress(raw or b'', mtime=0)).decode()})
        cases.append({'ticker': attempt['ticker'], 'observed_at': attempt['observed_at'],
                      'document': dict(doc), 'result': result, 'fetches': fetches})
    db.close()
    args.output.write_text(json.dumps({'version': 1, 'scope': 'AUTOMATIC_QUEUE_SAMPLE_OFFLINE_SOURCE_REGRESSION',
                                      'cases': cases}, indent=2) + '\n')


if __name__ == '__main__':
    main()
