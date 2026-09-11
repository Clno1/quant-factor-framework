"""Build a source-only, hash-addressed release; never package databases or secrets."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("NEW_ARCHIVE_PATH_REQUIRED")
    names = []
    for directory in ("src", "tests", "scripts", "configs", "deploy/systemd",
                      "reviews/2026-09-09-ep-llm-trial", "reviews/2026-09-10-ep-span-selection",
                      "reviews/2026-09-08-ep-official-sources"):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix in {".py", ".json", ".yaml", ".toml", ".md", ".service", ".timer"}:
                if not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(ROOT).parts):
                    names.append(path.relative_to(ROOT).as_posix())
    contents = {name: (ROOT / name).read_bytes() for name in sorted(set(names))}
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    encoded = json.dumps(manifest, sort_keys=True).encode()
    release_id = hashlib.sha256(encoded).hexdigest()[:16]
    contents["ep_release_manifest.json"] = encoded
    with tarfile.open(args.output, "w:gz") as archive:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            archive.addfile(info, io.BytesIO(data))
    print(json.dumps({"release_id": release_id, "files": len(manifest), "archive_bytes": args.output.stat().st_size,
                      "archive_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
