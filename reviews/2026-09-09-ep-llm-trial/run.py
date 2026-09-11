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
    key = validate_key(getpass.getpass("Selected provider API key (hidden): ").strip())
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
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--provider", choices=("openai", "kimi-cn", "kimi-intl"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure-key", help="Interactive hidden input; never connects to model API")
    commands.add_parser("plan", help="Read-only source and budget checks; never reads the key")
    commands.add_parser("status", help="Read-only budget journal summary")
    execute = commands.add_parser("run", help="Run exactly one approved document")
    execute.add_argument("ticker", choices=tuple(SOURCES))
    execute.add_argument("--execute", action="store_true")
    execute.add_argument("--retry-of", help="Explicit failed request key; same payload, new budget reservation")
    execute.add_argument("--read-timeout-seconds", type=int, default=60)
    from src.breakouts.ep.llm_batches import SCOPES
    for command in ("batch-plan", "batch-status", "batch-run"):
        sub = commands.add_parser(command)
        sub.add_argument("ticker", choices=tuple(SOURCES))
        if command == "batch-run":
            sub.add_argument("batch", choices=tuple(SCOPES))
            sub.add_argument("--execute", action="store_true")
    for command in ("span-plan", "span-run", "event-plan", "event-run"):
        sub = commands.add_parser(command)
        sub.add_argument("ticker", choices=tuple(SOURCES))
        if command.startswith("span-"):
            sub.add_argument("batch", choices=tuple(SCOPES))
        sub.add_argument("--paragraph-ids", required=True, help="Comma-separated archived paragraph IDs; manual scope, not full discovery")
        if command in {"span-run", "event-run"}:
            sub.add_argument("--execute", action="store_true")
            sub.add_argument("--expected-request-key", required=True)
    args = parser.parse_args()
    paid = args.command in {"run", "batch-run", "span-run", "event-run"}
    span_options = {"protocol": "span-selection", "paragraph_ids": args.paragraph_ids.split(",")} if args.command.startswith("span-") else {}
    if args.command.startswith("event-"):
        span_options = {"protocol": "event-interpretation", "paragraph_ids": args.paragraph_ids.split(",")}
    if paid and not args.execute:
        raise ValueError("EXPLICIT_EXECUTE_REQUIRED")
    if (paid or args.command == "configure-key") and not args.provider:
        raise ValueError("EXPLICIT_PROVIDER_REQUIRED")
    provider = args.provider or "openai"
    key_file = args.key_file or Path("/etc/quant/ep-llm-trial.key" if provider == "openai"
                                    else f"/etc/quant/ep-llm-trial-{provider}.key")
    if args.command == "configure-key":
        print(json.dumps(configure_key(key_file)))
        return 0

    from src.breakouts.ep.llm_provider import LlmSettings, create_transport
    from src.breakouts.ep.llm_service import plan_llm, run_llm, batch_status
    from src.breakouts.ep.store import EpStore

    # Ignore ambient model/budget overrides: this approval is for this fixed trial only.
    settings = LlmSettings(model="gpt-5.4-mini" if provider == "openai" else "kimi-k2.6", provider=provider, enabled=False,
        daily_microusd=3_000_000, monthly_microusd=10_000_000, total_microusd=10_000_000,
        max_output_tokens=8000 if args.command.startswith(("batch-", "span-")) else 4000,
        read_timeout_seconds=180 if args.command.startswith(("batch-", "span-", "event-")) else getattr(args, "read_timeout_seconds", 60))
    store = EpStore(DATABASE, read_only=True)
    summary = {"database": str(DATABASE), "model": settings.model, "provider": settings.provider,
        "total_limit_microusd": settings.total_microusd, "budget": budget_status(store),
        "delivery": "DISABLED_SHADOW_ONLY"}
    if args.command == "plan":
        summary["sources"] = {symbol: plan_llm(store, sid, settings) for symbol, sid in SOURCES.items()}
        summary["http_requests"] = 0
    elif args.command == "batch-plan":
        summary["batches"] = {name: plan_llm(store, SOURCES[args.ticker], settings, batch=name) for name in SCOPES}
        summary["all_batches_reserved_microusd"] = sum(p["budget_estimate"]["reserved_microusd"] for p in summary["batches"].values())
        summary["http_requests"] = 0
    elif args.command == "batch-status":
        summary.update(batch_status(store, SOURCES[args.ticker], settings))
    elif args.command in {"span-plan", "event-plan"}:
        summary["plan"] = plan_llm(store, SOURCES[args.ticker], settings, batch=getattr(args, "batch", None), **span_options)
        summary["http_requests"] = 0
    elif paid:
        if not args.execute:
            raise ValueError("EXPLICIT_EXECUTE_REQUIRED")
        if span_options:
            plan = plan_llm(store, SOURCES[args.ticker], settings, batch=getattr(args, "batch", None), **span_options)
            if plan["request_key"] != args.expected_request_key:
                raise ValueError("PLANNED_REQUEST_CHANGED")
            if plan["preflight"]["status"] in {"BILLING_REVIEW_REQUIRED", "BUDGET_EXHAUSTED", "JOURNAL_UPGRADE_REQUIRED"}:
                print(json.dumps({**summary, "plan": plan, "http_requests": 0}))
                return 2
        key = read_key(key_file)
        before = store.report()
        result = run_llm(EpStore(DATABASE), SOURCES[args.ticker], replace(settings, enabled=True),
                         create_transport(settings, key), retry_of=getattr(args, "retry_of", None), batch=getattr(args, "batch", None),
                         expected_request_key=getattr(args, "expected_request_key", None), **span_options)
        if store.report() != before:
            raise ValueError("CANDIDATE_SNAPSHOT_CHANGED")
        detail = result.get("result") or {}
        validation = detail.get("validation") or {}
        summary.update(ticker=args.ticker, status=result["status"], reused=result["reused"],
            http_attempts=result["external_requests"], request_key=result["request_key"],
            accepted_count=len(validation.get("accepted", [])), rejected_count=len(validation.get("rejected", [])),
            error=detail.get("error"), usage=detail.get("usage"), finish_reason=detail.get("finish_reason"),
            transport_diagnostics=detail.get("transport_diagnostics"),
            batch=detail.get("batch"), model_scope_status=validation.get("model_scope_status"),
            validation_status=validation.get("status"), rejected=validation.get("rejected"),
            claim_limit_reached=validation.get("claim_limit_reached"),
            protocol=detail.get("protocol", "claims"), coverage=validation.get("coverage"),
            budget=budget_status(EpStore(DATABASE, read_only=True)))
    print(json.dumps(summary, indent=2))
    return 0 if not paid or summary["status"] == "VALIDATED" else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, UnicodeError, EOFError):
        # Do not echo exceptions or a key pasted in an unexpected format.
        print(json.dumps({"status": "BLOCKED", "reason": "CHECK_PROVIDER_SOURCE_DATABASE_EXECUTE_FLAG_AND_PRIVATE_KEY_FILE"}))
        sys.exit(2)
