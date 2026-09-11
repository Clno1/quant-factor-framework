"""Compress public archived bytes; retain original hashes for offline regressions."""
import base64
import gzip
import hashlib
import json
from pathlib import Path
import sys


def build(source, destination):
    bundle = json.loads(Path(source).read_text())
    for case in bundle['cases']:
        for fetch in case['fetches']:
            raw = base64.b64decode(fetch.pop('raw_base64') or '')
            if raw:
                assert hashlib.sha256(raw).hexdigest() == fetch['result']['raw_sha256']
            fetch['gzip_base64'] = base64.b64encode(gzip.compress(raw, mtime=0)).decode()
    Path(destination).write_text(json.dumps(bundle, ensure_ascii=True, indent=2) + '\n')


if __name__ == '__main__':
    build(*sys.argv[1:])
