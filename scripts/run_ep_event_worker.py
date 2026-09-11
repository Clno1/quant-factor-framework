#!/usr/bin/env python3
"""EP event proposals and durable review, isolated from trading and notification services."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.event_worker import WorkerConfig, budget, cycle
from src.alerts.ep_event import notifications
from src.breakouts.ep.event_workflow import EventReviewStore, reviewed_report, render_reviewed
from src.breakouts.ep.store import EpStore
from src.utils.file_lock import file_lock
from src.utils.io import atomic_save_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    run = commands.add_parser("run")
    run.add_argument("--execute", action="store_true")
    commands.add_parser("status")
    report = commands.add_parser("report")
    report.add_argument("request_key")
    report.add_argument("--format", choices=("json", "text"), default="text")
    review = commands.add_parser("review")
    review.add_argument("request_key")
    review.add_argument("claim_id")
    review.add_argument("--binding-id", required=True)
    review.add_argument("--action", choices=("APPROVE", "REJECT", "REVOKE"), required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reason", required=True)
    review.add_argument("--expected-revision", type=int, required=True)
    review.add_argument("--confirm-source-support", action="store_true")
    args = parser.parse_args()
    if args.config.stat().st_size > 100_000:
        raise ValueError("WORKER_CONFIG_TOO_LARGE")
    config = WorkerConfig.model_validate_json(args.config.read_text())
    store = EpStore(config.database, read_only=True)
    if args.command == "plan":
        result = cycle(config)
    elif args.command == "run":
        if not args.execute or not config.enabled:
            raise ValueError("EXECUTE_AND_ENABLED_CONFIG_REQUIRED")
        with file_lock(Path(config.database).with_suffix(".event-worker.lock")):
            ingestion = None
            if config.collect_enabled:
                from src.breakouts.ep.event_ingest import ingest
                try:
                    ingestion = ingest(config)
                except Exception:
                    ingestion = {"status": "FAILED", "reason": "CHECK_FMP_SEC_ACCESS_AND_CONTACT_CONFIGURATION"}
            result = cycle(config, execute=True)
            result["ingestion"] = ingestion
            try:
                result["notifications"] = notifications(config, result)
            except (ValueError, OSError):
                result["notifications"] = {"status": "BLOCKED", "reason": "CHECK_PRIVATE_WEBHOOK_AND_EXPECTED_CHANNEL"}
            directory = Path(config.output_directory)
            filename = result["started_at"].replace(":", "-") + ".json"
            atomic_save_json(result, directory / "history" / filename)
            atomic_save_json(result, directory / "latest.json")
    elif args.command == "status":
        with store.connection() as db:
            rows = db.execute("""SELECT request_key, status, started_at, finished_at FROM ep_llm_calls
                WHERE json_extract(request_json, '$.protocol')='event-claims'
                ORDER BY started_at DESC LIMIT 20""").fetchall()
        result = {"budget": budget(store), "calls": [dict(row) for row in rows], "external_requests": 0,
                  "enabled": config.enabled, "delivery": "UNVERIFIED_AI_CONFIGURED" if config.delivery_enabled else "DISABLED"}
    elif args.command == "review":
        reviews = EventReviewStore(config.reviews_database, read_only=False)
        result = reviews.decide(store, args.request_key, args.claim_id, binding_id=args.binding_id,
                                action=args.action, reviewer=args.reviewer, reason=args.reason,
                                expected_revision=args.expected_revision, confirm_source_support=args.confirm_source_support)
    else:
        result = reviewed_report(store, EventReviewStore(config.reviews_database), args.request_key)
        if args.format == "text":
            print(render_reviewed(result))
            return 0
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Config/source/provider exceptions can contain private text. Never echo them.
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__,
                          "action": "CHECK_CONFIG_JOURNAL_KEY_PERMISSIONS_AND_REVIEW_BINDING"}), file=sys.stderr)
        sys.exit(1)
