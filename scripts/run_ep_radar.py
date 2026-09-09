#!/usr/bin/env python3
"""Collect once or inspect EP shadow state; deliberately no live/send mode."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.breakouts.ep.models import EpSettings, ticker, timestamp  # noqa: E402
from src.breakouts.ep.store import EpStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "ep" / "observations.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Bounded one-shot collection, never sends notifications")
    collect.add_argument("--start", required=True)
    collect.add_argument("--end", required=True)
    collect.add_argument("--max-pages", type=int, default=3)
    collect.add_argument("--max-profiles", type=int, default=20)
    collect.add_argument("--max-requests", type=int, default=40)
    collect.add_argument("--deadline-seconds", type=float, default=120)
    collect.add_argument("--include-etfs", action="store_true")
    enrich = sub.add_parser("enrich", help="Fetch and verify original sources; no LLM, signals or delivery")
    enrich.add_argument("--run-id")
    enrich.add_argument("--ticker")
    enrich.add_argument("--max-documents", type=int, default=5)
    enrich.add_argument("--max-http-requests", type=int, default=16)
    enrich.add_argument("--deadline-seconds", type=float, default=90)
    enrich.add_argument("--allow-host", action="append", default=[], help="Additional exact trusted HTTPS hostname")
    enrich.add_argument("--source-overrides", type=Path, help="Explicit JSON map of document IDs to verified alternate URLs")
    discover = sub.add_parser("discover", help="Bounded automatic SEC source discovery; shadow evidence only")
    discover.add_argument("--run-id")
    discover.add_argument("--ticker")
    discover.add_argument("--registry", type=Path, default=ROOT / "configs/ep_sources.json")
    discover.add_argument("--max-documents", type=int, default=10)
    discover.add_argument("--max-filings", type=int, default=3)
    discover.add_argument("--max-exhibits", type=int, default=3)
    discover.add_argument("--max-http-requests", type=int, default=30)
    discover.add_argument("--deadline-seconds", type=float, default=180)
    review_import = sub.add_parser("review-import", help="Record a human evidence review; never rates or sends")
    review_import.add_argument("path", type=Path)
    review_import.add_argument("--reviewer", required=True)
    for name in ("llm-plan", "llm-extract", "llm-history"):
        command = sub.add_parser(name, help="EP model proposal workflow; execution is explicitly gated")
        command.add_argument("source_id")
        if name != "llm-extract":
            command.add_argument("--as-of")
        if name == "llm-plan":
            command.add_argument("--model", choices=("gpt-5.4-mini", "gpt-5.4"))
        if name == "llm-extract":
            command.add_argument("--execute", action="store_true", help="Explicitly authorize one paid source extraction")
    for name in ("report", "explain", "sources", "source", "review-template", "reviews", "dossier", "fetches", "analyze", "analyses"):
        command = sub.add_parser(name, help="Offline, read-only inspection")
        if name in {"report", "explain", "sources", "dossier", "fetches", "analyze", "analyses"}:
            command.add_argument("--run-id")
        command.add_argument("--as-of", help="Timezone-aware timestamp; gates actual receipt/run completion")
        if name in {"explain", "dossier", "analyze", "analyses"}:
            command.add_argument("ticker")
        if name == "analyze":
            command.add_argument("--persist", action="store_true", help="Append an offline analysis snapshot to the EP database")
        if name in {"source", "review-template", "reviews"}:
            command.add_argument("source_id")
        if name == "review-template":
            command.add_argument("--max-paragraphs", type=int, default=40)
    args = parser.parse_args()
    try:
        if args.command in {"llm-plan", "llm-extract", "llm-history"}:
            if not args.db.is_file():
                raise FileNotFoundError("Collect and verify original EP sources first")
            as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if getattr(args, "as_of", None) else None
            if as_of:
                timestamp(as_of)
            if args.command == "llm-history":
                result = {"source_id": args.source_id, "calls": EpStore(args.db, read_only=True).llm_history(args.source_id, as_of=as_of)}
            else:
                from dataclasses import replace
                from src.breakouts.ep.llm_provider import LlmSettings, OpenAIResponsesTransport
                from src.breakouts.ep.llm_service import plan_llm, run_llm
                settings = LlmSettings.from_env()
                if args.command == "llm-plan":
                    if args.model:
                        settings = replace(settings, model=args.model)
                    result = plan_llm(EpStore(args.db, read_only=True), args.source_id, settings, as_of=as_of)
                else:
                    if not args.execute or not settings.enabled or not settings.model:
                        raise ValueError("Requires --execute, EP_LLM_ENABLED=true and EP_LLM_MODEL; no request was sent")
                    transport = OpenAIResponsesTransport(os.getenv("EP_LLM_API_KEY", ""))
                    result = run_llm(EpStore(args.db), args.source_id, settings, transport)
        elif args.command == "collect":
            from src.breakouts.ep.provider import FmpEpProvider
            from src.breakouts.ep.service import EpRadar
            settings = EpSettings(max_pages=args.max_pages, max_profiles=args.max_profiles,
                max_requests=args.max_requests, deadline_seconds=args.deadline_seconds, include_etfs=args.include_etfs)
            result = EpRadar(EpStore(args.db), FmpEpProvider(), settings).collect(args.start, args.end)
        elif args.command == "enrich":
            from src.data.public_articles import DEFAULT_ARTICLE_HOSTS, PublicArticleClient
            from src.breakouts.ep.enrichment import SourceEnricher
            if not args.db.is_file():
                raise FileNotFoundError("Collect EP evidence before enriching sources")
            client = PublicArticleClient(allowed_hosts=DEFAULT_ARTICLE_HOSTS | set(args.allow_host),
                max_requests=args.max_http_requests, deadline_seconds=args.deadline_seconds)
            overrides = None
            if args.source_overrides:
                with args.source_overrides.open("rb") as handle:
                    raw = handle.read(100_001)
                if len(raw) > 100_000:
                    raise ValueError("Source override map exceeds 100 KB")
                overrides = json.loads(raw)
            result = SourceEnricher(EpStore(args.db), client, max_documents=args.max_documents, source_overrides=overrides).run(
                args.run_id, symbol=ticker(args.ticker) if args.ticker else None)
        elif args.command == "discover":
            from src.breakouts.ep.discovery import OfficialSourceDiscovery, load_registry
            from src.data.sec_attachments import SecDisclosureClient
            if not args.db.is_file():
                raise FileNotFoundError("Collect EP evidence before discovering original sources")
            registry = load_registry(args.registry)
            client = SecDisclosureClient(contact_email=os.getenv("SEC_CONTACT_EMAIL"),
                max_requests=args.max_http_requests, deadline_seconds=args.deadline_seconds)
            result = OfficialSourceDiscovery(EpStore(args.db), client, registry, max_documents=args.max_documents,
                max_filings=args.max_filings, max_exhibits=args.max_exhibits).run(
                    args.run_id, symbol=ticker(args.ticker) if args.ticker else None)
        elif args.command == "review-import":
            if not args.db.is_file():
                raise FileNotFoundError("Collect and enrich EP evidence before importing a review")
            with args.path.open("rb") as handle:
                raw = handle.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("Review file exceeds 1 MB")
            payload = json.loads(raw)
            result = EpStore(args.db).save_review(payload, args.reviewer, datetime.now(timezone.utc))
        else:
            as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
            if as_of:
                timestamp(as_of)
            persist = args.command == "analyze" and args.persist
            if persist and not args.db.is_file():
                raise FileNotFoundError("Collect and enrich EP evidence before saving analysis")
            store = EpStore(args.db, read_only=not persist)
            if args.command == "analyze":
                from src.breakouts.ep.analysis import analyze_candidate
                result = store.analyze_and_save(args.ticker, datetime.now(timezone.utc), as_of=as_of, run_id=args.run_id) if persist else analyze_candidate(
                    store, args.ticker, as_of=as_of, run_id=args.run_id)
            elif args.command == "analyses":
                report = store.report(args.run_id, as_of=as_of)
                result = {"run_id": report.get("run_id"), "analyses": store.analysis_history(
                    report["run_id"], args.ticker, as_of=as_of) if report.get("run_id") else []}
            elif args.command == "explain":
                result = store.explain(args.ticker, as_of=as_of, run_id=args.run_id)
            elif args.command == "dossier":
                from src.breakouts.ep.dossier import candidate_dossier
                result = candidate_dossier(store, args.ticker, as_of=as_of, run_id=args.run_id)
            elif args.command == "fetches":
                report = store.report(args.run_id, as_of=as_of)
                result = {"run_id": report.get("run_id"), "fetches": store.fetch_history(report["run_id"], as_of=as_of)
                          if report.get("run_id") else []}
            elif args.command == "source":
                result = store.source_detail(args.source_id, as_of=as_of)
            elif args.command == "review-template":
                from src.breakouts.ep.fact_review import review_template
                result = review_template(store.source_detail(args.source_id, as_of=as_of), max_paragraphs=args.max_paragraphs)
            elif args.command == "reviews":
                store.source_detail(args.source_id, as_of=as_of)
                result = {"source_id": args.source_id, "reviews": store.review_history(source_id=args.source_id, as_of=as_of),
                          "eligible_for_rating": False}
            else:
                result = store.report(args.run_id, as_of=as_of)
                sources = store.source_report(result["run_id"], as_of=as_of) if result.get("run_id") else {"status": "NOT_ENRICHED", "sources": []}
                if args.command == "sources":
                    result = sources
                else:
                    result["source_enrichment"] = sources
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.command == "collect" and result["status"] != "COMPLETE_OBSERVATION":
            return 2
        if args.command in {"enrich", "discover"} and result["status"] != "SOURCE_PASS_COMPLETED":
            return 2
        if args.command == "llm-extract" and result["status"] != "VALIDATED":
            return 2
        return 0
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))
    except sqlite3.OperationalError:
        print(json.dumps({"status": "FAILED", "error_code": "SQLITE_ACCESS_ERROR",
            "action": "Use the project Python runtime and check database permissions/WAL compatibility; no writable fallback was attempted."}), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error_code": type(exc).__name__}), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
