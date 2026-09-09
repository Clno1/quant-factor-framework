"""Isolated PDF extraction for the one-shot source audit, with a parent wall timeout."""
import json
from pathlib import Path
import resource
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.breakouts.ep.pdf_source import parse_pdf


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
    path, output = map(Path, sys.argv[1:])
    if output.exists() or path.stat().st_size > 5_000_000:
        raise ValueError("New output and bounded input required")
    output.write_text(json.dumps(parse_pdf(path.read_bytes()), ensure_ascii=False))


if __name__ == "__main__":
    main()
