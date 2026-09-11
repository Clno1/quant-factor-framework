"""Read only: export three failed public-source chains, never keys or LLM records."""
import base64
import json
import sqlite3
import sys

SOURCE_IDS = [
    '1361e849-2832-4f58-9069-7a0132c7516f',
    'b6db8540-6362-4db4-8dbe-273bebf29bbd',
    '12efa04c-a2e9-493c-8632-8f4a20276937',
]


def main(path):
    db = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    output = []
    for sid in SOURCE_IDS:
        row = db.execute('SELECT * FROM ep_source_attempts WHERE source_id=?', (sid,)).fetchone()
        doc = db.execute('SELECT * FROM ep_documents WHERE document_id=? AND revision_id=?',
                         (row['document_id'], row['revision_id'])).fetchone()
        result = json.loads(row['payload_json'])
        fetches = []
        for step in result['discovery_steps']:
            fetch = db.execute('SELECT * FROM ep_source_fetches WHERE fetch_id=?', (step['fetch_id'],)).fetchone()
            meta = json.loads(fetch['payload_json'])
            raw = fetch['raw_body']
            if raw is None and meta.get('body_fetch_id'):
                raw = db.execute('SELECT raw_body FROM ep_source_fetches WHERE fetch_id=?',
                                 (meta['body_fetch_id'],)).fetchone()[0]
            fetches.append({'url': fetch['url'], 'result': meta,
                            'raw_base64': base64.b64encode(raw).decode() if raw else None})
        output.append({'ticker': row['ticker'], 'source_id': sid, 'observed_at': row['observed_at'],
                       'document': dict(doc), 'result': result, 'fetches': fetches})
    print(json.dumps({'version': 1, 'cases': output}))


if __name__ == '__main__':
    main(sys.argv[1])
