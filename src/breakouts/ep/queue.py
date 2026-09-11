"""Durable EP pipeline work, independent of the LLM ledger and delivery outbox."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .models import digest, encode, ticker, timestamp

STAGES = {"IDENTITY", "SOURCE", "ANALYSIS", "DELIVERY", "MARKET"}
TERMINAL = {"COMPLETE", "EXCLUDED", "EXPIRED"}


class PipelineQueue:
    def __init__(self, path, *, read_only=False):
        self.path = Path(path).resolve()
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError("EP_QUEUE_NOT_INITIALIZED")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names and "ep_pipeline_schema" not in names:
                raise ValueError("NOT_AN_EP_PIPELINE_DATABASE")
            if not names and not read_only:
                db.executescript("""
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE ep_pipeline_schema(version INTEGER NOT NULL);
                    INSERT INTO ep_pipeline_schema VALUES(1);
                    CREATE TABLE jobs(
                        job_id TEXT PRIMARY KEY, stage TEXT NOT NULL, ticker TEXT NOT NULL,
                        document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                        state TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL,
                        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, due_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                        lease_token TEXT, lease_until TEXT, result TEXT,
                        UNIQUE(stage, ticker, document_id, revision_id));
                    CREATE INDEX jobs_due ON jobs(stage, state, due_at, created_at);
                    CREATE INDEX jobs_ticker ON jobs(ticker, created_at);
                    CREATE TABLE transitions(
                        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs,
                        observed_at TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL);
                    CREATE TABLE checkpoints(
                        name TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL);
                """)
            version = db.execute('SELECT version FROM ep_pipeline_schema').fetchone()[0]
            if version not in {1, 2}:
                raise ValueError("UNSUPPORTED_EP_PIPELINE_SCHEMA")
            if not read_only:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS events(
                        event_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, descriptor TEXT NOT NULL,
                        first_seen TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS event_members(
                        event_id TEXT NOT NULL REFERENCES events, document_id TEXT NOT NULL,
                        revision_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                        PRIMARY KEY(event_id,document_id,revision_id));
                    CREATE INDEX IF NOT EXISTS event_ticker ON events(ticker,first_seen);
                    CREATE TABLE IF NOT EXISTS watch_history(
                        snapshot_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, observed_at TEXT NOT NULL,
                        payload TEXT NOT NULL, change_reason TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS watch_ticker ON watch_history(ticker,observed_at);
                    UPDATE ep_pipeline_schema SET version=2;
                ''')
            self.event_schema = not read_only or version == 2

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro" if self.read_only else str(self.path),
                             uri=self.read_only, timeout=10)
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

    def _writable(self):
        if self.read_only:
            raise ValueError("WRITABLE_EP_QUEUE_REQUIRED")

    @staticmethod
    def _row(row):
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value["result"] = json.loads(value["result"]) if value["result"] else None
        return value

    def enqueue(self, stage, symbol, document_id, revision_id, payload, now, expires_at):
        self._writable()
        if stage not in STAGES or not document_id or not revision_id:
            raise ValueError("INVALID_EP_JOB_IDENTITY")
        symbol = ticker(symbol)
        created, expiry = timestamp(now), timestamp(expires_at)
        state = 'EXPIRED' if expiry <= created else 'PENDING'
        reason = 'EVENT_RETENTION_EXPIRED' if state == 'EXPIRED' else 'QUEUED'
        job_id = digest([stage, symbol, document_id, revision_id])
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("""INSERT OR IGNORE INTO jobs
                (job_id,stage,ticker,document_id,revision_id,state,reason,payload,
                 created_at,updated_at,due_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, stage, symbol, document_id, revision_id, state, reason, encode(payload), created, created, created, expiry)).rowcount
            if count:
                db.execute("INSERT INTO transitions(job_id,observed_at,state,reason) VALUES(?,?,?,?)",
                           (job_id, created, state, reason))
        return job_id

    def checkpoint(self, name):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM checkpoints WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def enqueue_event(self, symbol, payload, now, expires_at):
        from .event_identity import event_descriptor
        self._writable()
        event = payload['event']
        descriptor = event_descriptor(symbol, event)
        eid = descriptor['event_id']
        # The member is durable before the job. Replaying identity work repairs
        # a crash between these idempotent transactions.
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            older = db.execute('''SELECT 1 FROM event_members WHERE event_id=? AND document_id=?
                AND revision_id!=? LIMIT 1''', (eid, event['document_id'], event['revision_id'])).fetchone()
            db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',
                       (eid, ticker(symbol), encode(descriptor), timestamp(now)))
            inserted = db.execute('INSERT OR IGNORE INTO event_members VALUES(?,?,?,?,?)',
                (eid, event['document_id'], event['revision_id'], timestamp(now), encode(payload))).rowcount
            # Reopen within this transaction so a crash cannot lose the revision
            # update after persisting the member. Running leases stay untouched.
            if inserted and older:
                job_id = digest(['SOURCE', ticker(symbol), eid, 'event-v1'])
                db.execute("""UPDATE jobs SET payload=json_set(payload,'$.member_generation',
                    COALESCE(json_extract(payload,'$.member_generation'),0)+1) WHERE job_id=?""", (job_id,))
                changed = db.execute('''UPDATE jobs SET state='PENDING',reason='SOURCE_REVISION_RECHECK',
                    updated_at=?,due_at=?,result=NULL WHERE job_id=? AND state='COMPLETE' AND expires_at>?''',
                    (timestamp(now), timestamp(now), job_id, timestamp(now))).rowcount
                if changed:
                    db.execute("INSERT INTO transitions(job_id,observed_at,state,reason) VALUES(?,?,'PENDING','SOURCE_REVISION_RECHECK')",
                               (job_id, timestamp(now)))
        return self.enqueue('SOURCE', symbol, eid, 'event-v1', {**payload, 'event_group': descriptor}, now, expires_at)

    def event_members(self, event_id, now):
        if not self.event_schema:
            return []
        with self.connection() as db:
            rows = db.execute('''SELECT payload FROM event_members WHERE event_id=? AND observed_at<=?
                ORDER BY observed_at,document_id,revision_id''', (event_id, timestamp(now))).fetchall()
        latest = {}
        for row in rows:
            payload = json.loads(row[0])
            latest[payload['event']['document_id']] = payload
        return list(latest.values())

    def consolidate_sources(self, now, *, limit=5000):
        """Migrate ready v1 article jobs without touching running or completed work."""
        count = 0
        for pending in self.ready('SOURCE', now, limit=limit):
            if pending['payload'].get('event_group') or not pending['payload'].get('event', {}).get('published_at'):
                continue
            lease = self.claim('SOURCE', now, job_id=pending['job_id'])
            if not lease:
                continue
            try:
                group = self.enqueue_event(lease['ticker'], lease['payload'], now,
                                           datetime.fromisoformat(lease['expires_at']))
                self.finish(lease, 'COMPLETE', 'GROUPED_INTO_EVENT_NOT_SOURCE_SUCCESS', now,
                            result={'event_job_id': group})
                count += 1
            except (ValueError, KeyError, TypeError):
                self.finish(lease, 'RETRY', 'EVENT_GROUPING_INPUT_INVALID', now)
        return count

    def save_checkpoint(self, name, payload, now):
        self._writable()
        with self.connection() as db:
            db.execute("""INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(name)
                          DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload""",
                       (name, timestamp(now), encode(payload)))

    def expire(self, now):
        self._writable()
        instant = timestamp(now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT job_id FROM jobs WHERE expires_at<=?
                AND state NOT IN ('COMPLETE','EXCLUDED','EXPIRED')
                AND (lease_until IS NULL OR lease_until<=?)""", (instant, instant)).fetchall()
            for row in rows:
                db.execute("""UPDATE jobs SET state='EXPIRED',reason='EVENT_RETENTION_EXPIRED',
                    updated_at=?,lease_token=NULL,lease_until=NULL WHERE job_id=?""", (instant, row[0]))
                db.execute("INSERT INTO transitions(job_id,observed_at,state,reason) VALUES(?,?,'EXPIRED','EVENT_RETENTION_EXPIRED')",
                           (row[0], instant))
        return len(rows)

    def ready(self, stage, now, *, limit=100):
        if stage not in STAGES or type(limit) is not int or not 1 <= limit <= 5000:
            raise ValueError("INVALID_EP_QUEUE_SCOPE")
        instant = timestamp(now)
        with self.connection() as db:
            rows = db.execute("""SELECT * FROM jobs WHERE stage=? AND due_at<=? AND expires_at>?
                AND (state IN ('PENDING','RETRY') OR (state='RUNNING' AND lease_until<=?))
                ORDER BY created_at,job_id LIMIT ?""", (stage, instant, instant, instant, limit)).fetchall()
        return [self._row(r) for r in rows]

    def claim(self, stage, now, *, lease_seconds=600, job_id=None):
        self._writable()
        if stage not in STAGES or not 1 <= lease_seconds <= 3600:
            raise ValueError("INVALID_EP_LEASE")
        instant = timestamp(now)
        token = str(uuid4())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""SELECT * FROM jobs WHERE stage=? AND due_at<=? AND expires_at>?
                AND (state IN ('PENDING','RETRY') OR (state='RUNNING' AND lease_until<=?))
                AND (? IS NULL OR job_id=?) ORDER BY created_at,job_id LIMIT 1""",
                (stage, instant, instant, instant, job_id, job_id)).fetchone()
            if row is None:
                return None
            reason = "EXPIRED_LEASE_RECLAIMED" if row['state'] == 'RUNNING' else "WORKER_CLAIMED"
            db.execute("""UPDATE jobs SET state='RUNNING',reason=?,updated_at=?,
                attempts=attempts+1,lease_token=?,lease_until=? WHERE job_id=?""",
                (reason, instant, token, timestamp(now + timedelta(seconds=lease_seconds)), row['job_id']))
            db.execute("INSERT INTO transitions(job_id,observed_at,state,reason) VALUES(?,?,'RUNNING',?)",
                       (row['job_id'], instant, reason))
            result = db.execute("SELECT * FROM jobs WHERE job_id=?", (row['job_id'],)).fetchone()
        return self._row(result)

    def finish(self, job, state, reason, now, *, result=None, retry_seconds=300):
        self._writable()
        if state not in TERMINAL | {"RETRY", "BLOCKED"} or not reason:
            raise ValueError("INVALID_EP_JOB_TRANSITION")
        if not 1 <= retry_seconds <= 86400:
            raise ValueError("INVALID_EP_RETRY_DELAY")
        instant = timestamp(now)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if state == 'COMPLETE' and job['stage'] == 'SOURCE' and job['payload'].get('event_group'):
                current = db.execute('SELECT payload FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
                if current and json.loads(current[0]).get('member_generation', 0) != job['payload'].get('member_generation', 0):
                    state, reason = 'RETRY', 'EVENT_REVISED_DURING_SOURCE_FETCH'
            changed = db.execute("""UPDATE jobs SET state=?,reason=?,updated_at=?,due_at=?,result=?,
                lease_token=NULL,lease_until=NULL WHERE job_id=? AND state='RUNNING'
                AND lease_token=? AND lease_until>?""",
                (state, reason, instant, timestamp(now + timedelta(seconds=retry_seconds)),
                 encode(result) if result is not None else None, job['job_id'], job['lease_token'], instant)).rowcount
            if changed != 1:
                raise ValueError("EP_JOB_LEASE_LOST")
            db.execute("INSERT INTO transitions(job_id,observed_at,state,reason) VALUES(?,?,?,?)",
                       (job['job_id'], instant, state, reason))

    def explain(self, symbol, *, limit=100):
        symbol = ticker(symbol)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("INVALID_EP_EXPLAIN_LIMIT")
        with self.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM jobs WHERE ticker=?", (symbol,)).fetchone()[0]
            rows = db.execute("SELECT * FROM jobs WHERE ticker=? ORDER BY created_at DESC,stage LIMIT ?",
                              (symbol, limit)).fetchall()
            jobs = []
            for row in rows:
                job = self._row(row)
                job.pop('payload')
                job.pop('lease_token')
                job['history'] = [dict(r) for r in db.execute("""SELECT observed_at,state,reason FROM transitions
                    WHERE job_id=? ORDER BY id DESC LIMIT 30""", (row['job_id'],))][::-1]
                jobs.append(job)
        events = []
        if self.event_schema:
            with self.connection() as db:
                for row in db.execute('SELECT * FROM events WHERE ticker=? ORDER BY first_seen DESC LIMIT ?', (symbol, limit)):
                    members = db.execute('SELECT document_id,revision_id,observed_at FROM event_members WHERE event_id=?',
                                         (row['event_id'],)).fetchall()
                    events.append({**json.loads(row['descriptor']), 'first_seen': row['first_seen'],
                                   'members': [dict(m) for m in members]})
        return {'ticker': symbol, 'jobs': jobs, 'events': events, 'total_jobs': count, 'truncated': count > limit,
                'reason': 'PIPELINE_HISTORY_AVAILABLE' if jobs else 'NOT_DISCOVERED_IN_OBSERVED_FEED_SCOPE',
                'discovery': self.checkpoint('discovery:last'),
                'market': self.checkpoint('market:' + symbol) or {'status': 'MARKET_NOT_CAPTURED'}, 'market_complete': False}

    def summary(self):
        with self.connection() as db:
            rows = db.execute("""SELECT stage,state,reason,COUNT(*) AS count,MIN(created_at) AS oldest_at
                                 FROM jobs GROUP BY stage,state,reason ORDER BY stage,state,reason""").fetchall()
        return {'groups': [dict(r) for r in rows], 'discovery': self.checkpoint('discovery:last'),
                'market_complete': False}

    def timeline(self, symbol, as_of):
        """Reconstruct states from transitions, never label current state as historical."""
        symbol, cutoff = ticker(symbol), timestamp(as_of)
        jobs = []
        with self.connection() as db:
            rows = db.execute('SELECT * FROM jobs WHERE ticker=? AND created_at<=? ORDER BY created_at,stage',
                              (symbol, cutoff)).fetchall()
            for row in rows:
                changes = [dict(r) for r in db.execute('''SELECT observed_at,state,reason FROM transitions
                    WHERE job_id=? AND observed_at<=? ORDER BY id''', (row['job_id'], cutoff))]
                if not changes:
                    continue
                jobs.append({'job_id': row['job_id'], 'stage': row['stage'], 'first_seen': row['created_at'],
                    **changes[-1], 'history': changes,
                    'age_seconds': (as_of - datetime.fromisoformat(row['created_at'])).total_seconds()})
            watches = [json.loads(r[0]) for r in db.execute('''SELECT payload FROM watch_history
                WHERE ticker=? AND observed_at<=? ORDER BY observed_at''', (symbol, cutoff))] if self.event_schema else []
        return {'ticker': symbol, 'as_of': cutoff, 'jobs': jobs, 'watch_history': watches,
                'coverage': 'OBSERVED_QUEUE_HISTORY_ONLY_NOT_WHOLE_MARKET_RECALL',
                'reason': 'HISTORY_AVAILABLE' if jobs or watches else 'NO_HISTORY_AT_ASOF'}

    def recover_delivery_handoffs(self, now, *, limit=20):
        """Recover completed analyses after enabling delivery or a handoff crash."""
        self._writable()
        if not 1 <= limit <= 100:
            raise ValueError('BOUNDED_HANDOFF_RECOVERY_REQUIRED')
        with self.connection() as db:
            rows = db.execute("""SELECT a.* FROM jobs a WHERE a.stage='ANALYSIS' AND a.state='COMPLETE'
                AND a.expires_at>? AND json_extract(a.result,'$.request_key') IS NOT NULL
                AND NOT EXISTS(SELECT 1 FROM jobs d WHERE d.stage='DELIVERY' AND d.ticker=a.ticker
                    AND d.document_id=a.document_id AND d.revision_id=a.revision_id)
                ORDER BY a.updated_at LIMIT ?""", (timestamp(now), limit)).fetchall()
        for row in rows:
            item = self._row(row)
            self.enqueue('DELIVERY', item['ticker'], item['document_id'], item['revision_id'],
                         {'request_key': item['result']['request_key'], 'recovered_from': item['job_id']},
                         now, datetime.fromisoformat(item['expires_at']))
        return len(rows)
