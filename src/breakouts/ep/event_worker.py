"""Bounded deployable event-review worker sharing the existing cumulative budget."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re
import stat
import time

from pydantic import Field, model_validator

from .event_workflow import EventReviewStore, reviewed_report
from .brief import source_brief
from .catalyst import current_commentary_window
from .llm_contract import StrictModel, prepare_request
from .llm_provider import LlmSettings, create_transport
from .llm_service import plan_llm, run_llm
from .models import timestamp
from .store import EpStore


class SourceJob(StrictModel):
    source_id: str = Field(min_length=1)
    paragraph_ids: list[str] = Field(min_length=1, max_length=12)


class WorkerConfig(StrictModel):
    database: str
    reviews_database: str
    output_directory: str
    key_file: str
    enabled: bool = False
    max_calls: int = Field(default=2, ge=1, le=2)
    max_sources: int = Field(default=3, ge=1, le=5)
    deadline_seconds: int = Field(default=300, ge=30, le=600)
    source_age_hours: int = Field(default=36, ge=1, le=72)
    jobs: list[SourceJob] = Field(default_factory=list, max_length=5)
    delivery_enabled: bool = False
    allow_unreviewed_ai: bool = False
    outbox_database: str = ""
    webhook_file: str = ""
    expected_channel_id: str = ""
    collect_enabled: bool = False

    @model_validator(mode="after")
    def validate_paths(self):
        paths = [Path(getattr(self, name)) for name in ("database", "reviews_database", "output_directory", "key_file")]
        paths += [Path(value) for value in (self.outbox_database, self.webhook_file) if value]
        if any(not p.is_absolute() for p in paths) or len({p.resolve() for p in paths}) != len(paths):
            raise ValueError("DISTINCT_ABSOLUTE_WORKER_PATHS_REQUIRED")
        if len({j.source_id for j in self.jobs}) != len(self.jobs):
            raise ValueError("DUPLICATE_SOURCE_JOBS")
        if self.collect_enabled and self.jobs:
            raise ValueError("CURRENT_COLLECTION_CANNOT_USE_ARCHIVED_JOB_LIST")
        if self.delivery_enabled and (not self.allow_unreviewed_ai or not self.outbox_database or not self.webhook_file
                                      or not re.fullmatch(r"[0-9]{1,30}", self.expected_channel_id)):
            raise ValueError("EXPLICIT_AI_DELIVERY_CHANNEL_AND_PRIVATE_WEBHOOK_REQUIRED")
        return self


def private_text(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="ascii") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("PRIVATE_KEY_OWNER_OR_MODE_INVALID")
        key = handle.read(4098).strip()
    return key


def private_key(path):
    key = private_text(path)
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,4096}", key):
        raise ValueError("PRIVATE_KEY_FORMAT_INVALID")
    return key


def settings(enabled=False):
    # This release remains inside the existing $10 experiment, independent of ambient env.
    return LlmSettings(model="kimi-k2.6", provider="kimi-cn", enabled=enabled,
                       daily_microusd=3_000_000, monthly_microusd=10_000_000, total_microusd=10_000_000,
                       max_output_tokens=4000, read_timeout_seconds=120)


def budget(store):
    if store.schema_version < 6:
        raise ValueError("EXISTING_LLM_BUDGET_JOURNAL_REQUIRED")
    with store.connection() as db:
        row = db.execute("SELECT COUNT(*), COALESCE(SUM(reserved_microusd),0) FROM ep_llm_calls").fetchone()
    return {"calls": row[0], "reserved_microusd": row[1], "total_limit_microusd": 10_000_000}


def select_jobs(store, config, now):
    if config.jobs:
        return list(config.jobs), {"mode": "EXPLICIT_ARCHIVED_SOURCES", "partial": True}
    with store.connection() as db:
        rows = db.execute("""SELECT a.source_id FROM ep_source_attempts a JOIN ep_source_runs r USING(batch_id)
            WHERE r.finished_at IS NOT NULL AND r.finished_at<=? AND a.observed_at>=? AND a.observed_at<=?
              AND json_extract(a.payload_json,'$.status')='DOCUMENT_MATCHED'
              AND json_extract(a.payload_json,'$.issuer_linkage')='REGISTERED_CIK_AND_CURRENT_SEC_TICKER_MATCH'
            ORDER BY a.observed_at DESC, a.source_id LIMIT 100""",
            (timestamp(now), timestamp(now - timedelta(hours=config.source_age_hours)), timestamp(now))).fetchall()
    jobs, skipped, seen = [], [], set()
    profiles = store.profiles(now)
    for row in rows:
        try:
            source = store.source_detail(row[0], as_of=now)
            profile = profiles.get(source["ticker"], {})
            identity = profile.get("profile") or {}
            received = profile.get("observed_at")
            age = now - datetime.fromisoformat(received) if received else None
            if (profile.get("status") != "OK" or identity.get("asset_type") not in {"STOCK", "ADR"}
                    or identity.get("exchange") not in {"NASDAQ", "NYSE", "AMEX"}
                    or identity.get("is_actively_trading") is not True or age is None
                    or not timedelta(0) <= age <= timedelta(hours=24)):
                skipped.append({"source_id": row[0], "reason": "FRESH_ELIGIBLE_SECURITY_IDENTITY_REQUIRED"})
                continue
            prepared = prepare_request(source)
            if not current_commentary_window(source_brief(source, now)["freshness"]):
                skipped.append({"source_id": row[0], "reason": "CURRENT_COMMENTARY_WINDOW_NOT_ESTABLISHED"})
                continue
            identity = (prepared["document_id"], prepared["text_revision"])
            if identity in seen:
                continue
            seen.add(identity)
            if len(jobs) >= config.max_sources:
                skipped.append({"source_id": row[0], "reason": "SOURCE_CAPACITY_LIMIT"})
                continue
            # A bounded leading excerpt, not an assertion of complete or relevant coverage.
            jobs.append(SourceJob(source_id=row[0], paragraph_ids=[p["id"] for p in prepared["untrusted_paragraphs"][:8]]))
        except (ValueError, KeyError, TypeError):
            skipped.append({"source_id": row[0], "reason": "SOURCE_NOT_ELIGIBLE"})
    return jobs, {"mode": "RECENT_RECEIPTS_NOT_CATALYST_FRESHNESS", "partial": True,
                  "query_scope": "MATCHED_ORIGINALS_ONLY_OTHER_FAILURES_IN_SOURCE_REPORT",
                  "examined": len(rows), "query_limit_reached": len(rows) == 100, "skipped": skipped}


def cycle(config, *, execute=False, transport_factory=create_transport, key_reader=private_key,
          clock=lambda: datetime.now(timezone.utc), monotonic=time.monotonic):
    start, ends = clock(), monotonic() + config.deadline_seconds
    store = EpStore(config.database, read_only=True)
    before = budget(store)
    jobs, selection = select_jobs(store, config, start)
    output, requests, transport, uncertain_attempts = [], 0, None, 0
    for job in jobs:
        item = {"source_id": job.source_id}
        output.append(item)
        attempted = False
        try:
            plan = plan_llm(store, job.source_id, settings(config.enabled), protocol="event-claims",
                            paragraph_ids=job.paragraph_ids, as_of=clock())
            item["plan"] = plan
            status = plan["preflight"]["status"]
            if status != "READY":
                item["status"] = status
                if status == "VALIDATED":
                    item["report"] = reviewed_report(store, EventReviewStore(config.reviews_database), plan["request_key"])
                continue
            if not execute or not config.enabled:
                item["status"] = "PLAN_ONLY"
                continue
            if requests + uncertain_attempts >= config.max_calls or monotonic() + settings().read_timeout_seconds > ends:
                item["status"] = "CAPACITY_OR_DEADLINE_DEFERRED"
                continue
            if transport is None:
                transport = transport_factory(settings(True), key_reader(config.key_file))
            attempted = True
            result = run_llm(EpStore(config.database), job.source_id, settings(True), transport,
                             protocol="event-claims", paragraph_ids=job.paragraph_ids,
                             expected_request_key=plan["request_key"], clock=clock)
            requests += result["external_requests"]
            attempted = False
            item["status"] = result["status"]
            if result["status"] == "VALIDATED":
                item["report"] = reviewed_report(store, EventReviewStore(config.reviews_database), result["request_key"])
        except (ValueError, KeyError, TypeError, OSError):
            item["status"] = "SOURCE_OR_CONFIGURATION_ERROR"
            if attempted:
                uncertain_attempts += 1
                break
    return {"version": "ep-event-worker-v1", "started_at": timestamp(start), "finished_at": timestamp(clock()),
            "status": "EMPTY_INPUT" if not jobs else "COMPLETED_WITH_PER_SOURCE_STATUS",
            "selection": selection, "items": output, "external_requests": requests,
            "uncertain_request_attempts": uncertain_attempts,
            "budget_before": before, "budget_after": budget(store),
            "delivery": "UNVERIFIED_AI_CONFIGURED" if config.delivery_enabled else "DISABLED", "eligible_for_rating": False}
