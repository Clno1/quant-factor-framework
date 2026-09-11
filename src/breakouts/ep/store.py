"""Isolated SQLite evidence revisions, receipt times and explainable shadow runs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .models import ALGORITHM_VERSION, CatalystSnapshot, digest, encode, ticker, timestamp


def _source_document_key(row):
    payload = json.loads(row['payload_json'])
    attachment = (payload.get('final_url') or row['source_id']) if payload.get('parent_source_id') else ''
    return row['document_id'], row['revision_id'], attachment


class EpStore:
    def __init__(self, path: str | Path, *, read_only: bool = False) -> None:
        self.path = Path(path).resolve()
        self.read_only = read_only
        if read_only and not self.path.is_file():
            raise FileNotFoundError("EP database does not exist; no collection has been run")
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "ep_schema" not in tables:
                raise ValueError("Refusing a non-EP database")
            if "ep_schema" in tables:
                if db.execute("SELECT version FROM ep_schema").fetchone()[0] not in {1, 2, 3, 4, 5, 6}:
                    raise ValueError("Unsupported EP schema version")
            elif read_only:
                raise ValueError("EP schema missing")
            else:
                db.executescript("""
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE ep_schema (version INTEGER NOT NULL);
                    INSERT INTO ep_schema VALUES (1);
                    CREATE TABLE ep_runs (
                        run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                        finished_at TEXT, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                        status TEXT NOT NULL, config_json TEXT NOT NULL, summary_json TEXT
                    );
                    CREATE TABLE ep_documents (
                        document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                        ticker TEXT NOT NULL, kind TEXT NOT NULL, event_date TEXT NOT NULL,
                        published_at TEXT, first_seen_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                        PRIMARY KEY (document_id, revision_id)
                    );
                    CREATE TABLE ep_observations (
                        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ep_runs,
                        document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        FOREIGN KEY(document_id, revision_id) REFERENCES ep_documents,
                        UNIQUE(run_id, document_id, revision_id, observed_at)
                    );
                    CREATE INDEX ep_observed_at ON ep_observations(observed_at, document_id);
                    CREATE TABLE ep_pages (
                        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ep_runs,
                        payload_json TEXT NOT NULL
                    );
                    CREATE TABLE ep_profiles (
                        id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ep_runs,
                        ticker TEXT NOT NULL, observed_at TEXT NOT NULL,
                        status TEXT NOT NULL, payload_json TEXT
                    );
                    CREATE TABLE ep_evaluations (
                        run_id TEXT NOT NULL REFERENCES ep_runs, ticker TEXT NOT NULL,
                        payload_json TEXT NOT NULL, PRIMARY KEY(run_id, ticker)
                    );
                """)
            self.schema_version = db.execute("SELECT version FROM ep_schema").fetchone()[0]
            if self.schema_version == 1 and not read_only:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS ep_source_runs (
                        batch_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ep_runs,
                        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                        config_json TEXT NOT NULL, summary_json TEXT
                    );
                    CREATE TABLE IF NOT EXISTS ep_source_contents (
                        content_id TEXT PRIMARY KEY, raw_sha256 TEXT NOT NULL,
                        raw_html BLOB NOT NULL, parsed_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS ep_source_attempts (
                        source_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES ep_source_runs,
                        ticker TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                        observed_at TEXT NOT NULL, content_id TEXT REFERENCES ep_source_contents,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(document_id, revision_id) REFERENCES ep_documents,
                        UNIQUE(batch_id, document_id, revision_id)
                    );
                    CREATE INDEX IF NOT EXISTS ep_source_lookup ON ep_source_attempts(document_id, revision_id, observed_at);
                    UPDATE ep_schema SET version=2;
                    COMMIT;
                """)
                self.schema_version = 2
            if self.schema_version == 2 and not read_only:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS ep_fact_reviews (
                        review_id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL REFERENCES ep_source_attempts(source_id),
                        recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ep_fact_review_source ON ep_fact_reviews(source_id, recorded_at);
                    UPDATE ep_schema SET version=3;
                    COMMIT;
                """)
                self.schema_version = 3
            if self.schema_version == 3 and not read_only:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS ep_source_fetches (
                        fetch_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES ep_source_runs,
                        url TEXT NOT NULL, received_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
                        raw_body BLOB, payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ep_fetch_url_time ON ep_source_fetches(url, recorded_at);
                    UPDATE ep_schema SET version=4;
                    COMMIT;
                """)
                self.schema_version = 4
            if self.schema_version == 4 and not read_only:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS ep_source_analyses (
                        analysis_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ep_runs,
                        ticker TEXT NOT NULL, recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS ep_analysis_history ON ep_source_analyses(run_id, ticker, recorded_at);
                    UPDATE ep_schema SET version=5;
                    COMMIT;
                """)
                self.schema_version = 5
            if self.schema_version == 5 and not read_only:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS ep_llm_calls (
                        request_key TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES ep_source_attempts,
                        started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                        budget_day TEXT NOT NULL, budget_month TEXT NOT NULL,
                        reserved_microusd INTEGER NOT NULL CHECK(reserved_microusd > 0),
                        request_json TEXT NOT NULL, result_json TEXT, response_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS ep_llm_budget ON ep_llm_calls(budget_day, budget_month);
                    UPDATE ep_schema SET version=6;
                    COMMIT;
                """)
                self.schema_version = 6

    def preview_llm_call(self, request_key, reserve, settings, now):
        """Consistent read-only snapshot, not a reservation or execution guarantee."""
        from datetime import timezone
        if self.schema_version < 6:
            return {"status": "JOURNAL_UPGRADE_REQUIRED", "would_reserve_microusd": 0}
        if type(reserve) is not int or reserve <= 0:
            raise ValueError("INVALID_LLM_RESERVATION")
        utc = now.astimezone(timezone.utc)
        day, month = utc.strftime("%Y-%m-%d"), utc.strftime("%Y-%m")
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT status FROM ep_llm_calls WHERE request_key=?", (request_key,)).fetchone()
            review = db.execute("SELECT 1 FROM ep_llm_calls WHERE status='BILLING_REVIEW_REQUIRED' LIMIT 1").fetchone()
            totals = db.execute("""SELECT COALESCE(SUM(CASE WHEN budget_day=? THEN reserved_microusd ELSE 0 END),0),
                COALESCE(SUM(CASE WHEN budget_month=? THEN reserved_microusd ELSE 0 END),0),
                COALESCE(SUM(reserved_microusd),0) FROM ep_llm_calls""", (day, month)).fetchone()
        limits = (settings.daily_microusd, settings.monthly_microusd, settings.total_microusd)
        exhausted = any(used + reserve > limit for used, limit in zip(totals, limits))
        status = row["status"] if row else "BILLING_REVIEW_REQUIRED" if review else "BUDGET_EXHAUSTED" if exhausted else "READY"
        return {"status": status, "existing_request": row is not None,
                "would_reserve_microusd": reserve if status == "READY" else 0,
                "used_microusd": dict(zip(("daily", "monthly", "total"), totals)),
                "limits_microusd": dict(zip(("daily", "monthly", "total"), limits)),
                "execution_must_recheck": True}

    def reserve_llm_call(self, request_key, source_id, request, reserve, settings, now):
        from datetime import timezone
        if self.read_only or self.schema_version < 6:
            raise ValueError("WRITABLE_LLM_JOURNAL_REQUIRED")
        if type(reserve) is not int or reserve <= 0:
            raise ValueError("INVALID_LLM_RESERVATION")
        self.source_detail(source_id, as_of=now)
        utc = now.astimezone(timezone.utc)
        day, month = utc.strftime("%Y-%m-%d"), utc.strftime("%Y-%m")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM ep_llm_calls WHERE request_key=?", (request_key,)).fetchone()
            if row:
                return {"reserved": False, "status": row["status"],
                        "result": json.loads(row["result_json"]) if row["result_json"] else None}
            if request.get("retry_of") is not None:
                parent = db.execute("SELECT * FROM ep_llm_calls WHERE request_key=?", (request["retry_of"],)).fetchone()
                if not parent or parent["status"] != "FAILED" or parent["source_id"] != source_id:
                    raise ValueError("LLM_RETRY_REQUIRES_FAILED_PARENT")
                previous = json.loads(parent["request_json"])
                failure = json.loads(parent["result_json"])["error"]
                retryable = {"LLM_TRANSPORT_FAILED", "LLM_CONNECT_TIMEOUT", "LLM_READ_TIMEOUT", "LLM_TIMEOUT",
                    "LLM_TLS_FAILED", "LLM_CONNECTION_FAILED", "LLM_ELAPSED_LIMIT_EXCEEDED", "LLM_HTTP_429",
                    "LLM_HTTP_500", "LLM_HTTP_502", "LLM_HTTP_503", "LLM_HTTP_504"}
                if failure not in retryable:
                    raise ValueError("LLM_FAILURE_NOT_RETRYABLE")
                logical_key = previous.get("logical_request_key", parent["request_key"])
                if (request.get("logical_request_key") != logical_key
                        or any(previous.get(k) != request.get(k) for k in ("source_request", "provider", "model"))
                        or request_key != digest({"logical_request_key": logical_key, "retry_of": parent["request_key"]})):
                    raise ValueError("LLM_RETRY_PAYLOAD_MISMATCH")
                request = {**request, "retry_number": previous.get("retry_number", 0) + 1}
                if request["retry_number"] > 2:
                    raise ValueError("LLM_MANUAL_RETRY_LIMIT")
            if db.execute("SELECT 1 FROM ep_llm_calls WHERE status='BILLING_REVIEW_REQUIRED' LIMIT 1").fetchone():
                return {"reserved": False, "status": "BILLING_REVIEW_REQUIRED", "result": None}
            daily = db.execute("SELECT COALESCE(SUM(reserved_microusd),0) FROM ep_llm_calls WHERE budget_day=?", (day,)).fetchone()[0]
            monthly = db.execute("SELECT COALESCE(SUM(reserved_microusd),0) FROM ep_llm_calls WHERE budget_month=?", (month,)).fetchone()[0]
            total = db.execute("SELECT COALESCE(SUM(reserved_microusd),0) FROM ep_llm_calls").fetchone()[0]
            if (daily + reserve > settings.daily_microusd or monthly + reserve > settings.monthly_microusd
                    or total + reserve > settings.total_microusd):
                return {"reserved": False, "status": "BUDGET_EXHAUSTED", "result": None}
            db.execute("INSERT INTO ep_llm_calls VALUES (?, ?, ?, NULL, 'RESERVED', ?, ?, ?, ?, NULL, NULL)",
                       (request_key, source_id, timestamp(now), day, month, reserve, encode(request)))
        return {"reserved": True, "status": "RESERVED", "result": None}

    def finish_llm_call(self, request_key, result, response, now):
        if self.read_only:
            raise ValueError("WRITABLE_LLM_JOURNAL_REQUIRED")
        with self.connection() as db:
            changed = db.execute("""UPDATE ep_llm_calls SET status=?, finished_at=?, result_json=?, response_json=?
                WHERE request_key=? AND status='RESERVED' AND started_at<=?""",
                (result["status"], timestamp(now), encode(result), encode(response) if response is not None else None,
                 request_key, timestamp(now))).rowcount
            if changed != 1:
                raise ValueError("LLM_RESERVATION_NOT_FINISHABLE")

    def llm_history(self, source_id, *, as_of=None):
        self.source_detail(source_id, as_of=as_of)
        if self.schema_version < 6:
            return []
        query = "SELECT * FROM ep_llm_calls WHERE source_id=?"
        args = [source_id]
        if as_of:
            query += " AND started_at<=?"
            args.append(timestamp(as_of))
        with self.connection() as db:
            rows = db.execute(query + " ORDER BY started_at, rowid", args).fetchall()
        results = []
        for row in rows:
            available = row["finished_at"] and (not as_of or row["finished_at"] <= timestamp(as_of))
            results.append({"request_key": row["request_key"], "started_at": row["started_at"],
                "finished_at": row["finished_at"] if available else None,
                "status": row["status"] if available else "RESERVED",
                "reserved_microusd": row["reserved_microusd"],
                "result": json.loads(row["result_json"]) if available else None})
        return results

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        uri = self.path.as_uri() + ("?mode=ro" if self.read_only else "?mode=rwc")
        db = sqlite3.connect(uri, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def start_run(self, start: str, end: str, config: dict[str, Any], now: datetime) -> str:
        run_id = str(uuid4())
        with self.connection() as db:
            db.execute("""UPDATE ep_runs SET status='INTERRUPTED', finished_at=?, summary_json=?
                          WHERE status='RUNNING'""", (timestamp(now), encode({
                              "status": "INTERRUPTED", "reason": "PREVIOUS_COLLECTOR_DID_NOT_FINISH",
                              "delivery": "DISABLED_SHADOW_ONLY"})))
            db.execute("INSERT INTO ep_runs VALUES (?, ?, NULL, ?, ?, 'RUNNING', ?, NULL)",
                       (run_id, timestamp(now), start, end, encode(config)))
        return run_id

    def save_page(self, run_id: str, page: dict[str, Any], documents: list[CatalystSnapshot]) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO ep_pages(run_id, payload_json) VALUES (?, ?)", (run_id, encode(page)))
            for doc in documents:
                db.execute("INSERT OR IGNORE INTO ep_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (doc.document_id, doc.revision_id, doc.ticker, doc.kind, doc.event_date,
                            doc.published_at, doc.observed_at, encode(doc.payload)))
                db.execute("""INSERT OR IGNORE INTO ep_observations
                    (run_id, document_id, revision_id, observed_at) VALUES (?, ?, ?, ?)""",
                           (run_id, doc.document_id, doc.revision_id, doc.observed_at))

    def documents(self, start: str, end: str, as_of: datetime) -> list[dict[str, Any]]:
        # Rank observations, not first-seen revisions: an issuer can revert a correction.
        with self.connection() as db:
            rows = db.execute("""
                WITH visible AS (
                    SELECT d.*, o.observed_at, ROW_NUMBER() OVER (
                        PARTITION BY d.document_id ORDER BY o.observed_at DESC, o.id DESC
                    ) AS position
                    FROM ep_observations o JOIN ep_documents d
                        ON d.document_id=o.document_id AND d.revision_id=o.revision_id
                    WHERE o.observed_at <= ?
                ) SELECT * FROM visible WHERE position=1 AND event_date BETWEEN ? AND ?
                  ORDER BY ticker, published_at, document_id
            """, (timestamp(as_of), start, end)).fetchall()
        return [{**{key: row[key] for key in row.keys() if key not in {"payload_json", "position"}},
                 "payload": json.loads(row["payload_json"])} for row in rows]

    def save_profile(self, run_id: str, symbol: str, now: datetime, status: str,
                     profile: dict[str, Any] | None) -> None:
        with self.connection() as db:
            db.execute("INSERT INTO ep_profiles(run_id, ticker, observed_at, status, payload_json) VALUES (?, ?, ?, ?, ?)",
                       (run_id, symbol, timestamp(now), status, encode(profile) if profile else None))

    def profiles(self, as_of: datetime) -> dict[str, dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("""WITH visible AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY observed_at DESC, id DESC) AS position
                FROM ep_profiles WHERE observed_at <= ?
            ) SELECT * FROM visible WHERE position=1""", (timestamp(as_of),)).fetchall()
        return {row["ticker"]: {"observed_at": row["observed_at"], "status": row["status"],
                               "profile": json.loads(row["payload_json"]) if row["payload_json"] else None}
                for row in rows}

    def finish_run(self, run_id: str, now: datetime, summary: dict[str, Any],
                   evaluations: list[dict[str, Any]]) -> None:
        with self.connection() as db:
            for item in evaluations:
                db.execute("INSERT INTO ep_evaluations VALUES (?, ?, ?)",
                           (run_id, item["ticker"], encode(item)))
            db.execute("UPDATE ep_runs SET finished_at=?, status=?, summary_json=? WHERE run_id=?",
                       (timestamp(now), summary["status"], encode(summary), run_id))

    def report(self, run_id: str | None = None, *, as_of: datetime | None = None) -> dict[str, Any]:
        with self.connection() as db:
            query = "SELECT * FROM ep_runs WHERE 1=1"
            params: list[Any] = []
            if run_id:
                query += " AND run_id=?"
                params.append(run_id)
            if as_of:
                query += " AND finished_at IS NOT NULL AND finished_at <= ?"
                params.append(timestamp(as_of))
            row = db.execute(query + " ORDER BY started_at DESC, rowid DESC LIMIT 1", params).fetchone()
            if row is None:
                return {"status": "NO_RUN_AVAILABLE_AS_OF", "candidates": [], "pages": []}
            result = dict(row)
            result["config"] = json.loads(result.pop("config_json"))
            result["summary"] = json.loads(result.pop("summary_json") or "{}")
            result["candidates"] = [json.loads(item[0]) for item in db.execute(
                "SELECT payload_json FROM ep_evaluations WHERE run_id=? ORDER BY ticker", (row["run_id"],))]
            result["pages"] = [json.loads(item[0]) for item in db.execute(
                "SELECT payload_json FROM ep_pages WHERE run_id=? ORDER BY id", (row["run_id"],))]
        return result

    def start_source_run(self, run_id: str, config: dict, now: datetime) -> str:
        batch_id = str(uuid4())
        with self.connection() as db:
            db.execute("UPDATE ep_source_runs SET status='INTERRUPTED', finished_at=? WHERE status='RUNNING'",
                       (timestamp(now),))
            db.execute("INSERT INTO ep_source_runs VALUES (?, ?, ?, NULL, 'RUNNING', ?, NULL)",
                       (batch_id, run_id, timestamp(now), encode(config)))
        return batch_id

    def save_source_attempt(self, batch_id: str, symbol: str, event: dict, result: dict,
                            now: datetime, *, raw: bytes | None = None, parsed: dict | None = None,
                            content_id: str | None = None) -> str:
        source_id = str(uuid4())
        with self.connection() as db:
            if raw is not None and parsed is not None:
                raw_sha = hashlib.sha256(raw).hexdigest()
                content_id = digest([raw_sha, parsed])
                db.execute("INSERT OR IGNORE INTO ep_source_contents VALUES (?, ?, ?, ?)",
                           (content_id, raw_sha, raw, encode(parsed)))
            db.execute("INSERT INTO ep_source_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (source_id, batch_id, symbol, event["document_id"], event["revision_id"],
                        timestamp(now), content_id, encode(result)))
        return source_id

    def finish_source_run(self, batch_id: str, summary: dict, now: datetime) -> None:
        with self.connection() as db:
            db.execute("UPDATE ep_source_runs SET finished_at=?, status=?, summary_json=? WHERE batch_id=?",
                       (timestamp(now), summary["status"], encode(summary), batch_id))

    def save_fetch(self, batch_id: str, url: str, result: dict, now: datetime, raw: bytes | None = None) -> str:
        fetch_id = str(uuid4())
        result = dict(result)
        if raw is not None:
            if len(raw) > 5_000_000:
                raise ValueError("Source response exceeds archive budget")
            result["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        received = result.get("received_at") or timestamp(now)
        received = timestamp(datetime.fromisoformat(received))
        if received > timestamp(now):
            raise ValueError("Source receipt cannot be in the future")
        with self.connection() as db:
            db.execute("INSERT INTO ep_source_fetches VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (fetch_id, batch_id, url, received, timestamp(now), raw, encode(result)))
        return fetch_id

    def cached_fetch(self, url: str, as_of: datetime) -> dict | None:
        if self.schema_version < 4:
            return None
        with self.connection() as db:
            row = db.execute("""SELECT f.* FROM ep_source_fetches f JOIN ep_source_runs r USING(batch_id)
                WHERE url=? AND f.recorded_at<=? AND f.received_at<=? AND r.finished_at<=?
                ORDER BY f.recorded_at DESC, f.rowid DESC LIMIT 1""",
                (url, timestamp(as_of), timestamp(as_of), timestamp(as_of))).fetchone()
        if row is None:
            return None
        result, raw = json.loads(row["payload_json"]), row["raw_body"]
        body_id = row["fetch_id"] if raw is not None else result.get("body_fetch_id")
        if raw is None and body_id:
            with self.connection() as db:
                body = db.execute("""SELECT f.raw_body FROM ep_source_fetches f JOIN ep_source_runs r USING(batch_id)
                    WHERE f.fetch_id=? AND f.recorded_at<=? AND f.received_at<=? AND r.finished_at<=?""",
                    (body_id, timestamp(as_of), timestamp(as_of), timestamp(as_of))).fetchone()
            raw = body[0] if body else None
        if result.get("status") == "FETCHED" and (raw is None or hashlib.sha256(raw).hexdigest() != result.get("raw_sha256")):
            return None
        return {"fetch_id": row["fetch_id"], "body_fetch_id": body_id, "raw": raw, "result": result}

    def fetch_history(self, run_id: str, *, as_of: datetime | None = None) -> list[dict]:
        if self.schema_version < 4:
            return []
        query = """SELECT f.fetch_id, f.batch_id, f.url, f.received_at, f.recorded_at, f.payload_json
            FROM ep_source_fetches f JOIN ep_source_runs r USING(batch_id)
            WHERE r.run_id=? AND r.finished_at IS NOT NULL"""
        args = [run_id]
        if as_of:
            query += " AND r.finished_at<=? AND f.recorded_at<=? AND f.received_at<=?"
            args += [timestamp(as_of)] * 3
        with self.connection() as db:
            rows = db.execute(query + " ORDER BY f.recorded_at, f.rowid", args).fetchall()
        return [{"fetch_id": row["fetch_id"], "batch_id": row["batch_id"], "url": row["url"],
                 "received_at": row["received_at"], "recorded_at": row["recorded_at"],
                 **json.loads(row["payload_json"])} for row in rows]

    def cached_source(self, document_id: str, revision_id: str, as_of: datetime) -> dict | None:
        if self.schema_version < 2:
            return None
        with self.connection() as db:
            row = db.execute("""SELECT a.* FROM ep_source_attempts a JOIN ep_source_runs r USING(batch_id)
                WHERE document_id=? AND revision_id=? AND r.finished_at<=? AND a.observed_at<=?
                  AND json_extract(a.payload_json, '$.retrieved_at') IS NOT NULL
                ORDER BY a.observed_at DESC, a.rowid DESC LIMIT 1""",
                (document_id, revision_id, timestamp(as_of), timestamp(as_of))).fetchone()
        if row is None:
            return None
        return {**dict(row), "result": json.loads(row["payload_json"])}

    def source_report(self, run_id: str, *, as_of: datetime | None = None) -> dict:
        if self.schema_version < 2:
            return {"status": "NOT_ENRICHED", "sources": []}
        with self.connection() as db:
            query = """SELECT * FROM ep_source_runs WHERE run_id=?
                AND COALESCE(json_extract(config_json, '$.route'), '') != 'OFFICIAL_ATTACHMENT'"""
            args = [run_id]
            if as_of:
                query += " AND finished_at IS NOT NULL AND finished_at<=?"
                args.append(timestamp(as_of))
            row = db.execute(query + " ORDER BY started_at DESC, rowid DESC LIMIT 1", args).fetchone()
            if row is None:
                return {"status": "NOT_ENRICHED", "sources": []}
            result = {"batch_id": row["batch_id"], "run_id": row["run_id"], "status": row["status"],
                      "started_at": row["started_at"], "finished_at": row["finished_at"],
                      "config": json.loads(row["config_json"]), "summary": json.loads(row["summary_json"] or "{}")}
            source_query = """SELECT a.*, r.finished_at AS source_finished_at FROM ep_source_attempts a
                JOIN ep_source_runs r USING(batch_id) WHERE r.run_id=?"""
            source_args = [run_id]
            if as_of:
                source_query += " AND r.finished_at IS NOT NULL AND r.finished_at<=? AND a.observed_at<=?"
                source_args.extend([timestamp(as_of), timestamp(as_of)])
            latest = {}
            for item in db.execute(source_query + " ORDER BY a.observed_at, a.rowid", source_args):
                latest[_source_document_key(item)] = item
            result["summary_scope"] = "LATEST_BATCH_ONLY"
            result["sources"] = [{"source_id": item["source_id"], "ticker": item["ticker"],
                                  "source_batch_id": item["batch_id"], "source_finished_at": item["source_finished_at"],
                                  "document_id": item["document_id"], "revision_id": item["revision_id"],
                                  "observed_at": item["observed_at"], "content_id": item["content_id"],
                                  **json.loads(item["payload_json"])} for item in sorted(
                                      latest.values(), key=lambda item: (item["ticker"], item["document_id"]))]
        return result

    def aligned_sources(self, run_id: str, symbol: str, *, as_of: datetime | None = None) -> list[dict]:
        if self.schema_version < 2:
            return []
        query = """SELECT a.* FROM ep_source_attempts a JOIN ep_source_runs r USING(batch_id)
            WHERE r.run_id=? AND a.ticker=? AND r.finished_at IS NOT NULL
              AND json_extract(a.payload_json, '$.status')='DOCUMENT_MATCHED'"""
        args = [run_id, symbol]
        if as_of:
            query += " AND a.observed_at<=? AND r.finished_at<=?"
            args += [timestamp(as_of)] * 2
        with self.connection() as db:
            rows = db.execute(query + " ORDER BY a.observed_at DESC, a.rowid DESC", args).fetchall()
        latest = {}
        for row in rows:
            latest.setdefault(_source_document_key(row), row["source_id"])
        return [self.source_detail(source_id, as_of=as_of) for source_id in latest.values()]

    def source_detail(self, source_id: str, *, as_of: datetime | None = None) -> dict:
        if self.schema_version < 2:
            raise ValueError("No original-source schema in this database")
        with self.connection() as db:
            query = """SELECT a.*, r.finished_at, r.run_id, d.published_at, c.raw_sha256, c.parsed_json
                FROM ep_source_attempts a JOIN ep_source_runs r USING(batch_id)
                JOIN ep_documents d ON a.document_id=d.document_id AND a.revision_id=d.revision_id
                LEFT JOIN ep_source_contents c USING(content_id)
                WHERE source_id=? AND r.finished_at IS NOT NULL"""
            args = [source_id]
            if as_of:
                query += " AND r.finished_at<=? AND a.observed_at<=?"
                args.extend([timestamp(as_of), timestamp(as_of)])
            row = db.execute(query, args).fetchone()
        if row is None:
            raise ValueError("Source is not available at this observation time")
        return {"source_id": source_id, "document_id": row["document_id"], "revision_id": row["revision_id"],
                "ticker": row["ticker"], "run_id": row["run_id"], "published_at": row["published_at"],
                "observed_at": row["observed_at"], "raw_sha256": row["raw_sha256"],
                "result": json.loads(row["payload_json"]),
                "parsed": json.loads(row["parsed_json"]) if row["parsed_json"] else None,
                "human_reviews": self.review_history(source_id=source_id, as_of=as_of)}

    def save_review(self, payload: dict, reviewer: str, now: datetime) -> dict:
        from .fact_review import validate_review
        if self.read_only:
            raise ValueError("Review import requires a writable EP database")
        if not isinstance(payload, dict) or not isinstance(payload.get("source_id"), str):
            raise ValueError("Review source_id required")
        source = self.source_detail(payload["source_id"], as_of=now)
        reviewed = validate_review(source, payload, reviewer, now)
        review_id = digest({key: value for key, value in reviewed.items() if key != "recorded_at"})
        reviewed["review_id"] = review_id
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO ep_fact_reviews VALUES (?, ?, ?, ?)",
                       (review_id, source["source_id"], timestamp(now), encode(reviewed)))
            row = db.execute("SELECT payload_json FROM ep_fact_reviews WHERE review_id=?", (review_id,)).fetchone()
        return json.loads(row[0])

    def analyze_and_save(self, symbol: str, now: datetime, *, run_id=None, as_of=None) -> dict:
        from .analysis import analyze_candidate
        if self.read_only:
            raise ValueError("Analysis persistence requires a writable EP database")
        evidence_as_of = as_of or now
        if timestamp(evidence_as_of) > timestamp(now):
            raise ValueError("Analysis evidence time cannot be in the future")
        result = analyze_candidate(self, symbol, run_id=run_id, as_of=evidence_as_of)
        if not result["run_id"]:
            raise ValueError("Collect EP evidence before saving analysis")
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO ep_source_analyses VALUES (?, ?, ?, ?, ?)",
                       (result["analysis_id"], result["run_id"], result["ticker"], timestamp(now), encode(result)))
        return result

    def analysis_history(self, run_id: str, symbol: str, *, as_of=None) -> list[dict]:
        if self.schema_version < 5:
            return []
        query = """SELECT a.recorded_at, a.payload_json FROM ep_source_analyses a
            JOIN ep_runs r USING(run_id) WHERE a.run_id=? AND a.ticker=? AND r.finished_at IS NOT NULL"""
        args = [run_id, ticker(symbol)]
        if as_of:
            query += " AND a.recorded_at<=? AND r.finished_at<=?"
            args.extend([timestamp(as_of)] * 2)
        with self.connection() as db:
            return [{"recorded_at": row["recorded_at"], "analysis": json.loads(row["payload_json"])}
                    for row in db.execute(query + " ORDER BY a.recorded_at, a.rowid", args)]

    def review_history(self, *, source_id: str | None = None, run_id: str | None = None,
                       symbol: str | None = None, as_of: datetime | None = None) -> list[dict]:
        if self.schema_version < 3:
            return []
        if not source_id and not run_id:
            raise ValueError("Review history requires source_id or run_id")
        query = """SELECT f.payload_json FROM ep_fact_reviews f
            JOIN ep_source_attempts a USING(source_id) JOIN ep_source_runs r USING(batch_id)
            WHERE r.finished_at IS NOT NULL"""
        args = []
        for column, value in (("f.source_id", source_id), ("r.run_id", run_id), ("a.ticker", symbol)):
            if value is not None:
                query += f" AND {column}=?"
                args.append(value)
        if as_of:
            query += " AND f.recorded_at<=? AND a.observed_at<=? AND r.finished_at<=?"
            args.extend([timestamp(as_of)] * 3)
        with self.connection() as db:
            return [json.loads(row[0]) for row in db.execute(query + " ORDER BY f.recorded_at, f.rowid", args)]

    def explain(self, symbol: str, *, as_of: datetime | None = None, run_id: str | None = None) -> dict[str, Any]:
        symbol = ticker(symbol)
        report = self.report(run_id, as_of=as_of)
        candidate = next((row for row in report["candidates"] if row["ticker"] == symbol), None)
        sources = self.source_report(report["run_id"], as_of=as_of) if report.get("run_id") else {"status": "NOT_ENRICHED", "sources": []}
        sources["sources"] = [item for item in sources["sources"] if item["ticker"] == symbol]
        return {"ticker": symbol, "run_id": report.get("run_id"), "run_status": report["status"],
                "window": {"start": report.get("start_date"), "end": report.get("end_date")},
                "scope": report.get("config", {}).get("scope"),
                "run_finished_at": report.get("finished_at"),
                "as_of": timestamp(as_of) if as_of else None, "algorithm_version": ALGORITHM_VERSION,
                "candidate": candidate, "reason": "EVALUATED" if candidate else
                    ("RUN_NOT_FINISHED" if report["status"] == "RUNNING" else
                     "NO_RUN_AVAILABLE_AS_OF" if report["status"] == "NO_RUN_AVAILABLE_AS_OF" else
                     "NOT_FOUND_IN_FETCHED_SCOPE"),
                "coverage": report.get("summary", {}).get("coverage", []),
                "pages": report["pages"], "absence_is_not_no_catalyst": True,
                "source_enrichment": sources,
                "human_reviews": self.review_history(run_id=report["run_id"], symbol=symbol, as_of=as_of)
                    if report.get("run_id") else [],
                "delivery": "DISABLED_SHADOW_ONLY"}
