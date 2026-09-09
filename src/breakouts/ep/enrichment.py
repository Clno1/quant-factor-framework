"""Explicit source enrichment, independent of quote loops and immutable collection reports."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .models import timestamp
from .source_verifier import PARSER_VERSION, parse_article, verify_document
from .store import EpStore


class SourceEnricher:
    def __init__(self, store: EpStore, client: Any, *, max_documents: int = 5,
                 clock: Callable = lambda: datetime.now(timezone.utc), source_overrides: dict | None = None):
        if type(max_documents) is not int or not 1 <= max_documents <= 50:
            raise ValueError("max_documents must be between 1 and 50")
        self.store, self.client = store, client
        self.max_documents, self.clock = max_documents, clock
        if source_overrides is not None and (not isinstance(source_overrides, dict) or
                any(not isinstance(key, str) or not isinstance(value, str) for key, value in source_overrides.items())):
            raise ValueError("Source overrides must map document IDs to HTTPS URLs")
        self.source_overrides = {key: client.validate_url(value) for key, value in (source_overrides or {}).items()}

    def run(self, run_id: str | None = None, *, symbol: str | None = None) -> dict:
        from src.utils.file_lock import file_lock
        if self.store.read_only:
            raise ValueError("Source enrichment requires a writable EP database")
        with file_lock(self.store.path.with_suffix(".sources.lock")):
            return self._run(run_id, symbol)

    def _run(self, run_id: str | None, symbol: str | None) -> dict:
        report = self.store.report(run_id)
        if report["status"] not in {"PARTIAL", "COMPLETE_OBSERVATION"}:
            raise ValueError("Source enrichment requires a completed observation run")
        available = {event["document_id"] for candidate in report["candidates"] for event in candidate["events"]
                     if "url" in event["evidence"]}
        if set(self.source_overrides) - available:
            raise ValueError("Source override references a document outside this collection run")
        config = {"version": "ep-sources-v1a.1", "parser_version": PARSER_VERSION,
                  "max_documents": self.max_documents, "ticker_scope": symbol,
                  "allowed_hosts": sorted(self.client.allowed_hosts), "max_http_requests": self.client.max_requests,
                  "source_overrides": self.source_overrides,
                  "mode": "SHADOW", "llm": "NOT_CONFIGURED", "delivery": "DISABLED_SHADOW_ONLY"}
        batch = self.store.start_source_run(report["run_id"], config, self.clock())
        initial_requests = self.client.requests
        attempted = 0
        cache_hits = 0
        counts = Counter()
        seen = set()
        try:
            for candidate in report["candidates"]:
                if symbol and candidate["ticker"] != symbol:
                    continue
                events = sorted(candidate["events"], key=lambda item: (
                    item["event_type_hint"] in {"UNKNOWN", "LEGAL_NOTICE"}, item.get("published_at") or ""))
                for event in events:
                    if "url" not in event["evidence"] or (event["document_id"], event["revision_id"]) in seen:
                        continue
                    seen.add((event["document_id"], event["revision_id"]))
                    original_url = event["evidence"]["url"]
                    requested_url = self.source_overrides.get(event["document_id"], original_url)
                    if event["event_type_hint"] == "LEGAL_NOTICE" or candidate["status"] == "EXCLUDED":
                        result = {"status": "EXCLUDED_FROM_SOURCE_QUEUE"}
                    elif attempted >= self.max_documents:
                        result = {"status": "SOURCE_DOCUMENT_BUDGET_EXCEEDED"}
                    else:
                        cached = self.store.cached_source(event["document_id"], event["revision_id"], self.clock())
                        if cached and cached["result"].get("requested_url", cached["result"].get("final_url")) == requested_url:
                            original = cached["result"].get("retrieved_at")
                            try:
                                age = self.clock() - datetime.fromisoformat(original)
                            except (ValueError, TypeError):
                                age = timedelta(days=2)
                            cached_status = cached["result"].get("status")
                            ttl = timedelta(hours=24) if cached_status in {
                                "DOCUMENT_MATCHED", "SOURCE_HTTP_401", "SOURCE_HTTP_403", "SOURCE_HTTP_429",
                                "ROBOTS_DISALLOWED"
                            } else timedelta(minutes=15)
                            if cached_status in {"URL_POLICY_REJECTED", "NON_PUBLIC_ADDRESS_REJECTED",
                                                 "SOURCE_REQUEST_BUDGET_EXCEEDED", "SOURCE_TIME_BUDGET_EXCEEDED"}:
                                ttl = timedelta(0)
                            if timedelta(0) <= age < ttl and cached["result"].get("parser_version") == PARSER_VERSION:
                                result = {**cached["result"], "cache_used": True, "reused_from_source_id": cached["source_id"]}
                                if cached["content_id"]:
                                    detail = self.store.source_detail(cached["source_id"], as_of=self.clock())
                                    if detail["parsed"] and detail["parsed"].get("status") == "EXTRACTED":
                                        result["verification"] = verify_document(event, detail["parsed"], candidate["identity"])
                                        result["status"] = result["verification"]["status"]
                                self.store.save_source_attempt(batch, candidate["ticker"], event, result, self.clock(),
                                                               content_id=cached["content_id"])
                                counts[result["status"]] += 1
                                cache_hits += 1
                                continue
                        attempted += 1
                        fetched = self.client.fetch(requested_url)
                        result = {key: value for key, value in fetched.items() if key != "html"}
                        result.update(retrieved_at=timestamp(self.clock()), parser_version=PARSER_VERSION,
                                      requested_url=requested_url, original_url=original_url,
                                      source_route="EXPLICIT_OVERRIDE" if event["document_id"] in self.source_overrides else "ORIGINAL",
                                      cache_used=False, llm="NOT_CONFIGURED", delivery="DISABLED_SHADOW_ONLY")
                        raw, parsed = fetched.get("html"), None
                        if fetched["status"] == "FETCHED":
                            try:
                                parsed = parse_article(raw)
                                result["verification"] = verify_document(event, parsed, candidate["identity"])
                                result["status"] = result["verification"]["status"]
                                result.update(text_revision=parsed["text_revision"], body_characters=parsed["characters"],
                                              paragraph_count=len(parsed["paragraphs"]))
                            except Exception:
                                result["status"] = "SOURCE_PARSE_ERROR"
                                parsed = {"parser_version": PARSER_VERSION, "status": "SOURCE_PARSE_ERROR", "paragraphs": []}
                        self.store.save_source_attempt(batch, candidate["ticker"], event, result, self.clock(), raw=raw, parsed=parsed)
                        counts[result["status"]] += 1
                        continue
                    result.update(requested_url=requested_url, original_url=original_url)
                    self.store.save_source_attempt(batch, candidate["ticker"], event, result, self.clock())
                    counts[result["status"]] += 1
            partial = any(status not in {"DOCUMENT_MATCHED", "EXCLUDED_FROM_SOURCE_QUEUE"} for status in counts)
            status = "NO_SOURCE_TARGETS" if not counts else "PARTIAL_SOURCES" if partial else "SOURCE_PASS_COMPLETED"
            summary = {"status": status, "counts": dict(counts), "attempted_documents": attempted, "cache_hits": cache_hits,
                       "http_requests": self.client.requests - initial_requests, "ratings_enabled": False, "llm": "NOT_CONFIGURED",
                       "delivery": "DISABLED_SHADOW_ONLY", "completeness_claimed": False}
            self.store.finish_source_run(batch, summary, self.clock())
        except Exception as exc:
            self.store.finish_source_run(batch, {"status": "FAILED", "error_code": type(exc).__name__}, self.clock())
            raise
        return self.store.source_report(report["run_id"])
