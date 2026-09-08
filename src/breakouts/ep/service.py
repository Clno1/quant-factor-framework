"""Bounded, one-shot EP collection. No daemon, market trigger, LLM or delivery."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
import time
from typing import Any, Callable

from .classifier import classify
from .models import ALGORITHM_VERSION, EpSettings, NEW_YORK, digest, normalize_evidence, ticker, timestamp
from .provider import EpProvider
from .ranker import evaluate
from .store import EpStore


class CollectionBudget:
    def __init__(self, settings: EpSettings, monotonic: Callable[[], float]) -> None:
        self.settings = settings
        self.clock = monotonic
        self.ends_at = monotonic() + settings.deadline_seconds
        self.requests = 0
        self.stop_reason: str | None = None

    def call(self, function: Callable[..., Any], *args: Any) -> tuple[Any, str]:
        remaining = self.ends_at - self.clock()
        if self.stop_reason:
            return None, self.stop_reason
        if remaining <= 0:
            return None, "TIME_BUDGET_EXCEEDED"
        if self.requests >= self.settings.max_requests:
            return None, "REQUEST_BUDGET_EXCEEDED"
        self.requests += 1
        try:
            value = function(*args, timeout=min(remaining, self.settings.request_timeout_seconds))
            return value, "OK"
        except Exception as exc:
            # Persist codes only: provider exception text can contain sensitive URLs.
            http = getattr(getattr(exc, "response", None), "status_code", None)
            if http in {401, 403, 429}:
                self.stop_reason = f"PROVIDER_HTTP_{http}"
                return None, self.stop_reason
            return None, f"PROVIDER_ERROR_{type(exc).__name__}"


class EpRadar:
    def __init__(self, store: EpStore, provider: EpProvider, settings: EpSettings | None = None,
                 *, clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.store = store
        self.provider = provider
        self.settings = settings or EpSettings()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic

    def collect(self, start: str, end: str) -> dict[str, Any]:
        if self.store.read_only:
            raise ValueError("Collection requires a writable EP store")
        from src.utils.file_lock import file_lock
        with file_lock(self.store.path.with_suffix(".collection.lock")):
            return self._collect(start, end)

    def _collect(self, start: str, end: str) -> dict[str, Any]:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first.isoformat() != start or last.isoformat() != end or first > last:
            raise ValueError("Invalid YYYY-MM-DD collection window")
        if (last - first).days >= self.settings.max_days:
            raise ValueError("Collection window exceeds max_days")
        now = self.clock()
        timestamp(now)
        if last > now.astimezone(NEW_YORK).date():
            raise ValueError("Future collection windows are not supported")
        run_id = self.store.start_run(start, end, {**asdict(self.settings), "mode": "SHADOW",
            "algorithm_version": ALGORITHM_VERSION,
            "scope": getattr(self.provider, "scope", "INJECTED_PROVIDER_SCOPE_UNVERIFIED")}, now)
        budget = CollectionBudget(self.settings, self.monotonic)
        coverage: list[dict[str, Any]] = []
        try:
            for feed in ("stock", "press"):
                self._articles(run_id, feed, start, end, budget, coverage)
            day = first
            while day <= last:
                key = day.isoformat()
                rows, status = budget.call(self.provider.calendar, key)
                count, invalid = self._page(run_id, "calendar", key, 0, rows, status, "CALENDAR", key, key)
                if status == "OK" and count >= 4000:
                    status = "CALENDAR_LIMIT_REACHED"
                if status == "OK" and invalid:
                    status = "INVALID_RECORDS"
                coverage.append({"feed": "calendar", "day": key, "status": status, "rows": count})
                day += timedelta(days=1)
            cutoff = self.clock()
            documents = self.store.documents(start, end, cutoff)
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for document in documents:
                grouped[document["ticker"]].append(document)
            identities = self.store.profiles(cutoff)
            profiles_requested = 0
            evaluations = []
            def priority(symbol: str) -> int:
                hints = {classify(doc)["event_type_hint"] for doc in grouped[symbol]}
                if hints - {"UNKNOWN", "LEGAL_NOTICE", "EARNINGS_CALENDAR"}:
                    return 0
                if "EARNINGS_CALENDAR" in hints:
                    return 1
                return 2

            ordered = sorted(grouped, key=lambda symbol: (
                priority(symbol),
                identities.get(symbol, {}).get("observed_at", ""), symbol))
            for symbol in ordered:
                cached = identities.get(symbol)
                profile = None
                profile_observed_at = None
                identity_status = "PROFILE_BUDGET_EXCEEDED"
                if cached and cached["status"] == "OK" and (
                    cutoff - datetime.fromisoformat(cached["observed_at"])
                ) < timedelta(hours=24):
                    profile, identity_status = cached["profile"], "CACHED_PROFILE_UNDER_24H"
                    profile_observed_at = cached["observed_at"]
                elif profiles_requested < self.settings.max_profiles:
                    previous_count = budget.requests
                    profile, identity_status = budget.call(self.provider.profile, symbol)
                    profiles_requested += budget.requests - previous_count
                    if identity_status == "OK":
                        if not profile:
                            identity_status = "PROFILE_NOT_FOUND"
                        elif not isinstance(profile, dict) or profile.get("ticker") != symbol:
                            profile, identity_status = None, "PROFILE_IDENTITY_MISMATCH"
                    if budget.requests > previous_count:
                        observed = self.clock()
                        profile_observed_at = timestamp(observed)
                        self.store.save_profile(run_id, symbol, observed, identity_status, profile)
                evaluation = evaluate(symbol, grouped[symbol], profile,
                    identity_status=identity_status, include_etfs=self.settings.include_etfs)
                evaluation["identity_observed_at"] = profile_observed_at
                evaluations.append(evaluation)
            completed = {"OK", "END_OF_FEED", "WINDOW_BOUNDARY_REACHED"}
            partial = any(item["status"] not in completed for item in coverage)
            deferred = sum(item["identity"] is None for item in evaluations)
            summary = {"status": "PARTIAL" if partial or deferred else "COMPLETE_OBSERVATION",
                "coverage": coverage, "requests": budget.requests, "candidate_count": len(evaluations),
                "identity_pending_count": deferred, "profiles_requested": profiles_requested,
                "market_coverage_proven": False, "historical_first_seen_reconstructed": False,
                "fulltext_retrieval": "NOT_IMPLEMENTED", "llm": "NOT_CONFIGURED",
                "price_confirmation": "NOT_IMPLEMENTED", "delivery": "DISABLED_SHADOW_ONLY"}
            self.store.finish_run(run_id, self.clock(), summary, evaluations)
        except Exception as exc:
            self.store.finish_run(run_id, self.clock(), {"status": "FAILED", "coverage": coverage,
                "error_code": type(exc).__name__, "delivery": "DISABLED_SHADOW_ONLY"}, [])
            raise
        return self.store.report(run_id)

    def _page(self, run_id: str, feed: str, partition: str, page: int, rows: Any, status: str,
              kind: str, start: str, end: str) -> tuple[int, int]:
        received = self.clock()
        documents = []
        rejected = []
        out_of_window = 0
        if status == "OK" and (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)):
            status = "INVALID_PAYLOAD"
            rejected.append({"reason": status})
        if status == "OK":
            for index, row in enumerate(rows):
                try:
                    doc = normalize_evidence(kind, row, received)
                    if not start <= doc.event_date <= end:
                        if kind == "CALENDAR":
                            raise ValueError("CALENDAR_DATE_MISMATCH")
                        out_of_window += 1
                        continue
                    documents.append(doc)
                except (ValueError, TypeError, OverflowError) as exc:
                    try:
                        symbol = ticker(row.get("symbol"))
                    except ValueError:
                        symbol = None
                    known = {"CALENDAR_DATE_MISMATCH", "INVALID_TICKER", "INVALID_FINANCIAL_NUMBER",
                             "INVALID_HEADLINE", "INVALID_SOURCE_URL", "PUBLICATION_TIME_MISSING", "FUTURE_PUBLICATION"}
                    reason = str(exc) if str(exc) in known else "INVALID_EVIDENCE_CONTRACT"
                    rejected.append({"row": index, "ticker": symbol, "reason": reason,
                        "source_symbol": str(row.get("symbol") or "")[:128],
                        "source_published_date": str(row.get("publishedDate") or row.get("date") or "")[:128],
                        "source_title": str(row.get("title") or "")[:300]})
        count = len(rows) if isinstance(rows, list) else 0
        self.store.save_page(run_id, {"feed": feed, "partition": partition, "page": page,
            "received_at": timestamp(received), "status": status, "row_count": count,
            "accepted_count": len(documents), "out_of_window_count": out_of_window,
            "rejected": rejected}, documents)
        return count, len(rejected)

    def _articles(self, run_id: str, feed: str, start: str, end: str,
                  budget: CollectionBudget, coverage: list[dict[str, Any]]) -> None:
        seen: set[str] = set()
        total = 0
        invalid_total = 0
        status = "PAGINATION_LIMIT_REACHED"
        previous_oldest: datetime | None = None
        ordered = True
        for page in range(self.settings.max_pages):
            rows, request_status = budget.call(self.provider.articles, feed, page, self.settings.page_size)
            count, invalid = self._page(run_id, feed, "latest", page, rows, request_status, "ARTICLE", start, end)
            total += count
            invalid_total += invalid
            if request_status != "OK":
                status = request_status
                break
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                status = "INVALID_RECORDS"
                break
            fingerprint = digest(rows)
            if fingerprint in seen:
                status = "REPEATED_PAGE"
                break
            seen.add(fingerprint)
            times = []
            for row in rows:
                try:
                    value = str(row.get("publishedDate") or "")
                    if "T" not in value and " " not in value:
                        raise ValueError("Missing publication time")
                    raw = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    times.append(raw.replace(tzinfo=NEW_YORK) if raw.tzinfo is None else raw)
                except ValueError:
                    ordered = False
            if times:
                ordered = ordered and times == sorted(times, reverse=True)
                ordered = ordered and (previous_oldest is None or max(times) <= previous_oldest)
                previous_oldest = min(times)
            if ordered and times and min(times).astimezone(NEW_YORK).date().isoformat() < start:
                status = "WINDOW_BOUNDARY_REACHED"
                break
            if count < self.settings.page_size:
                status = "END_OF_FEED"
                break
        coverage.append({"feed": feed, "status": "INVALID_RECORDS" if invalid_total else status,
                         "pagination_status": status, "rows": total,
                         "invalid_rows": invalid_total, "ordering_verified_in_sample": ordered})
