"""Run review reproductions in a temporary copy containing tracked files only.

The assertions confirm the defects observed at the reviewed commit; a future
fix should cause the corresponding assertion to fail. No production worker is
started. Individual experiment scripts use synthetic input and isolated stores.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
    here = Path(__file__).resolve().parent
    report = here.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=here.parents[2])
    parser.add_argument("--output-dir", type=Path, default=report / "evidence" / "reproduced")
    parser.add_argument("--node", default=shutil.which("node"))
    args = parser.parse_args()
    source = args.source_root.resolve()
    output = args.output_dir.resolve()
    coverage = json.loads((report / "reading_coverage.json").read_text())
    selected = [x for x in coverage["files"] if x["status"] == "full_text_read"]
    for entry in selected:
        path = source / entry["path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Reviewed source has changed: {entry['path']}")
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="quant_fresh_review_") as scratch:
        scratch = Path(scratch)
        snapshot = scratch / "source"
        for entry in selected:
            destination = snapshot / entry["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / entry["path"], destination)
        # Preserve experiment logic; replace only this session's absolute I/O paths.
        previous = "/private/tmp/quant_fresh_audit_20260905"
        generated = scratch / "experiments"
        generated.mkdir()
        for path in here.glob("repro_*"):
            if path.suffix not in {".py", ".js"}:
                continue
            text = path.read_text()
            for old in ("repo_trading", "repo_signals", "repo_data", "repo"):
                text = text.replace(previous + "/" + old, str(snapshot))
            text = text.replace(previous, str(output))
            (generated / path.name).write_text(text)
        env = dict(os.environ, PYTHONPATH=str(snapshot), MPLCONFIGDIR=str(scratch / "mpl"))
        commands = [
            [sys.executable, str(generated / "repro_data_factors_native.py")],
            [sys.executable, str(generated / "repro_signals_groups.py")],
            [sys.executable, str(generated / "repro_trading_web_numpy.py"), "--normal"],
            [sys.executable, str(generated / "repro_root.py")],
            [sys.executable, str(generated / "repro_template_names.py")],
        ]
        if args.node:
            commands.append([args.node, str(generated / "repro_template_names.js")])
        else:
            results.append({"experiment": "template JavaScript execution", "status": "SKIPPED_NO_NODE"})
        for command in commands:
            name = Path(command[1]).name
            result = subprocess.run(command, cwd=snapshot, env=env, capture_output=True, text=True)
            (output / (name + ".log")).write_text(result.stdout + result.stderr)
            row = {"experiment": name, "exit_code": result.returncode}
            results.append(row)
            print(json.dumps(row), flush=True)
    # Root cases retain per-case diagnostics even if the script itself exits zero.
    root_result = output / "repro_root_result.json"
    if root_result.exists():
        for name, value in json.loads(root_result.read_text()).items():
            if "harness_error" in value:
                results.append({"experiment": name, "exit_code": 1, **value})
    (output / "run_summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    return int(any(x.get("exit_code", 0) != 0 for x in results))


if __name__ == "__main__":
    raise SystemExit(main())
