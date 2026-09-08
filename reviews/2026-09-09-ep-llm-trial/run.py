"""Manual four-document trial. No schedules, discovery, ratings or delivery."""
from __future__ import annotations

import argparse
from dataclasses import replace
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
DATABASE = ROOT / "data" / "ep" / "llm_trial_20260909.sqlite3"
SOURCES = {
    "GTLB": "a7ff3ffe-9687-49bc-ad0c-0470c08058c7",
    "ANF": "5f5c24b9-acb6-4ed8-9a48-e66abd3b02ef",
    "NYAX": "38907f0d-b120-41dd-8d14-223815c8b86f",
    "PLAB": "4ee87559-2cdc-4917-b05f-1f5354a4054d",
}


def validate_key(key):
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,4096}", key):
        raise ValueError("INVALID_KEY_FORMAT")
    return key


def configure_key(path):
    if path.exists() or path.is_symlink():
        raise ValueError("KEY_FILE_ALREADY_EXISTS_NOT_OVERWRITTEN")
    key = validate_key(getpass.getpass("OpenAI API key (hidden): ").strip())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(key + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return {"key_configured": True, "key_file": str(path), "http_requests": 0}


def read_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="ascii") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("KEY_FILE_REQUIRES_OWNER_AND_MODE_0600")
        return validate_key(handle.read(4098).strip())


def budget_status(store):
    if store.schema_version < 6:
        return {"calls": 0, "reserved_microusd": 0, "statuses": {}}
    with store.connection() as db:
        row = db.execute("SELECT COUNT(*), COALESCE(SUM(reserved_microusd),0) FROM ep_llm_calls").fetchone()
        statuses = dict(db.execute("SELECT status, COUNT(*) FROM ep_llm_calls GROUP BY status").fetchall())
    return {"calls": row[0], "reserved_microusd": row[1], "statuses": statuses}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, default=Path("/etc/quant/ep-llm-trial.key"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure-key", help="Interactive hidden input; never connects to model API")
    commands.add_parser("plan", help="Read-only source and budget checks; never reads the key")
    commands.add_parser("status", help="Read-only budget journal summary")
    execute = commands.add_parser("run", help="Run exactly one approved document")
    execute.add_argument("ticker", choices=tuple(SOURCES))
    execute.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "configure-key":
        print(json.dumps(configure_key(args.key_file)))
        return 0

    from src.breakouts.ep.llm_provider import LlmSettings, OpenAIResponsesTransport
    from src.breakouts.ep.llm_service import plan_llm, run_llm
    from src.breakouts.ep.store import EpStore

    # Ignore ambient model/budget overrides: this approval is for this fixed trial only.
    settings = LlmSettings(model="gpt-5.4-mini", enabled=False,
        daily_microusd=3_000_000, monthly_microusd=10_000_000, total_microusd=10_000_000)
    store = EpStore(DATABASE, read_only=True)
    summary = {"database": str(DATABASE), "model": settings.model,
        "total_limit_microusd": settings.total_microusd, "budget": budget_status(store),
        "delivery": "DISABLED_SHADOW_ONLY"}
    if args.command == "plan":
        summary["sources"] = {symbol: plan_llm(store, sid, settings) for symbol, sid in SOURCES.items()}
        summary["http_requests"] = 0
    elif args.command == "run":
        if not args.execute:
            raise ValueError("EXPLICIT_EXECUTE_REQUIRED")
        key = read_key(args.key_file)
        before = store.report()
        result = run_llm(EpStore(DATABASE), SOURCES[args.ticker], replace(settings, enabled=True),
                         OpenAIResponsesTransport(key))
        if store.report() != before:
            raise ValueError("CANDIDATE_SNAPSHOT_CHANGED")
        detail = result.get("result") or {}
        validation = detail.get("validation") or {}
        summary.update(ticker=args.ticker, status=result["status"], reused=result["reused"],
            http_attempts=result["external_requests"], request_key=result["request_key"],
            accepted_count=len(validation.get("accepted", [])), rejected_count=len(validation.get("rejected", [])),
            error=detail.get("error"), usage=detail.get("usage"), budget=budget_status(EpStore(DATABASE, read_only=True)))
    print(json.dumps(summary, indent=2))
    return 0 if args.command != "run" or summary["status"] == "VALIDATED" else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, UnicodeError, EOFError):
        # Do not echo exceptions or a key pasted in an unexpected format.
        print(json.dumps({"status": "BLOCKED", "reason": "CHECK_SOURCE_DATABASE_EXECUTE_FLAG_AND_PRIVATE_KEY_FILE"}))
        sys.exit(2)
