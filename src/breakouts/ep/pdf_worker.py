"""PDF decoding in a disposable, resource-limited process, never the quote loop."""
import json
from pathlib import Path
import subprocess
import sys
import time


def parse_pdf_bounded(raw):
    if not isinstance(raw, bytes) or not raw.startswith(b'%PDF-') or len(raw) > 5_000_000:
        return {'status': 'PDF_BYTES_INVALID', 'paragraphs': []}
    try:
        with subprocess.Popen([sys.executable, '-m', 'src.breakouts.ep.pdf_worker'], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              cwd=Path(__file__).resolve().parents[3]) as process:
            end, data = time.monotonic() + 20, raw
            try:
                while True:
                    try:
                        output, _ = process.communicate(data, timeout=.2)
                        break
                    except subprocess.TimeoutExpired:
                        data = None
                        if time.monotonic() >= end:
                            raise ValueError('PDF_DECODE_TIMEOUT')
                        if sys.platform == 'darwin':
                            check = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(process.pid)],
                                                   capture_output=True, timeout=1, check=False)
                            if check.returncode == 0 and int(check.stdout.strip()) > 384 * 1024:
                                raise ValueError('PDF_MEMORY_LIMIT')
            except BaseException:
                process.kill()
                process.communicate()
                raise
        if process.returncode or len(output) > 2_000_000:
            raise ValueError('PDF_RESOURCE_OR_DECODE_ERROR')
        return json.loads(output)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return {'status': 'PDF_RESOURCE_OR_DECODE_ERROR', 'paragraphs': []}


def main():
    import logging
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    if sys.platform != 'darwin':
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 ** 2, 384 * 1024 ** 2))
    logging.disable(logging.CRITICAL)
    from .pdf_source import parse_pdf
    try:
        parsed = parse_pdf(sys.stdin.buffer.read(5_000_001))
        parsed['title'] = next((p['text'] for p in parsed['paragraphs'] if len(p['text']) < 250), '')
        from .models import digest
        parsed['text_revision'] = digest({k: v for k, v in parsed.items() if k != 'text_revision'})
        print(json.dumps(parsed))
    except Exception:
        print(json.dumps({'status': 'PDF_RESOURCE_OR_DECODE_ERROR', 'paragraphs': []}))


if __name__ == '__main__':
    main()
