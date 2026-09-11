"""Durable human decisions bound to immutable model claims; no notification transport."""
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3

from .llm_event_claims import prepare_atomic_packet, validate_atomic
from .llm_event_context import prepare_context_packet, validate_context
from .llm_provider import extract_kimi_response, extract_response
from .models import digest, timestamp
from .brief import source_brief
from .identity import current_profile


def call_report(store, request_key):
    if not re.fullmatch(r"[a-f0-9]{64}", request_key):
        raise ValueError("INVALID_REQUEST_KEY")
    with store.connection() as db:
        row = db.execute("SELECT * FROM ep_llm_calls WHERE request_key=?", (request_key,)).fetchone()
    if row is None:
        raise ValueError("EVENT_CALL_NOT_FOUND")
    journal = json.loads(row["request_json"])
    if journal.get("protocol") not in {'event-claims', 'event-context'}:
        raise ValueError("EVENT_CLAIMS_PROTOCOL_REQUIRED")
    base = {"request_key": request_key, "source_id": row["source_id"], "status": row["status"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "delivery": "CONTROLLED_BY_RUNTIME", "eligible_for_rating": False}
    if row["status"] != "VALIDATED":
        return {**base, "claims": [], "rejected": [], "reviewable": False}
    packet = journal["event_packet"]
    source = store.source_detail(row["source_id"])
    contextual = journal['protocol'] == 'event-context'
    current = (prepare_context_packet if contextual else prepare_atomic_packet)(
        source, [p['paragraph_id'] for p in packet['request']['untrusted_blocks']])
    if current != packet:
        raise ValueError("EVENT_SOURCE_CHANGED_REVIEW_INVALID")
    body = json.loads(row["response_json"])
    raw, _ = (extract_response if journal["provider"] == "openai" else extract_kimi_response)(body)
    validation = (validate_context if contextual else validate_atomic)(packet, raw)
    if contextual:
        base.update(protocol='event-context', sections=validation['sections'])
    brief = source_brief(source, datetime.now(timezone.utc))
    profile = store.profiles(datetime.now(timezone.utc)).get(source["ticker"], {})
    identity = profile.get("profile") or {}
    security_eligible = (profile.get("status") == "OK" and identity.get("asset_type") in {"STOCK", "ADR"}
                         and identity.get("exchange") in {"NASDAQ", "NYSE", "AMEX"}
                         and identity.get("is_actively_trading") is True
                         and current_profile(profile, datetime.now(timezone.utc)))
    claims = [{**c, "binding_id": digest({"request_key": request_key, "packet_hash": packet["packet_hash"],
                                         "validator": validation["version"], "claim": c})}
              for c in validation["accepted"]]
    return {**base, "ticker": source["ticker"], "document_id": source["document_id"],
            "text_revision": source["parsed"]["text_revision"], "freshness": brief["freshness"],
            "security_eligible": security_eligible,
            "source_url": source["result"]["final_url"],
            "source_observed_at": source["observed_at"], "provider_published_at": source["published_at"],
            "context": "ARCHIVED_DISCLOSURE_NOT_A_CURRENT_MARKET_ALERT", "coverage": validation["coverage"],
            "claims": claims, "rejected": validation["rejected"], "reviewable": bool(claims),
            "content_status": "UNVERIFIED_PROPOSALS_AVAILABLE" if claims else "ALL_PROPOSALS_BLOCKED_OR_EMPTY",
            "human_review_required_for_unverified_delivery": False,
            "semantic_support_verified": False}


class EventReviewStore:
    def __init__(self, path, *, read_only=True):
        self.path = Path(path).resolve()
        self.read_only = read_only
        if not self.path.exists() and read_only:
            return
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "event_review_schema" not in tables:
                raise ValueError("NOT_AN_EVENT_REVIEW_DATABASE")
            if "event_review_schema" not in tables:
                if read_only:
                    raise ValueError("EVENT_REVIEW_SCHEMA_REQUIRED")
                db.executescript("""
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE event_review_schema(version INTEGER NOT NULL);
                    INSERT INTO event_review_schema VALUES (1);
                    CREATE TABLE event_decisions(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL,
                        request_key TEXT NOT NULL, claim_id TEXT NOT NULL,
                        action TEXT NOT NULL CHECK(action IN ('APPROVE','REJECT','REVOKE')),
                        recorded_at TEXT NOT NULL, reviewer TEXT NOT NULL, reason TEXT NOT NULL,
                        decision_key TEXT NOT NULL UNIQUE);
                    CREATE INDEX event_decision_binding ON event_decisions(binding_id, id);
                """)
            if db.execute("SELECT version FROM event_review_schema").fetchone()[0] != 1:
                raise ValueError("UNKNOWN_EVENT_REVIEW_SCHEMA")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if self.read_only else "?mode=rwc"), uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def latest(self, binding_id):
        if not self.path.exists():
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM event_decisions WHERE binding_id=? ORDER BY id DESC LIMIT 1", (binding_id,)).fetchone()
        return dict(row) if row else None

    def decide(self, store, request_key, claim_id, *, binding_id, action, reviewer, reason, expected_revision,
               confirm_source_support=False, now=None):
        if self.read_only or action not in {"APPROVE", "REJECT", "REVOKE"}:
            raise ValueError("WRITABLE_REVIEW_ACTION_REQUIRED")
        if not re.fullmatch(r"[\w.@-]{1,80}", reviewer) or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
            raise ValueError("REVIEWER_AND_REASON_REQUIRED")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("INVALID_REVIEW_REVISION")
        report = call_report(store, request_key)
        claim = next((c for c in report["claims"] if c["claim_id"] == claim_id), None)
        if claim is None or claim["binding_id"] != binding_id:
            raise ValueError("CLAIM_BLOCKED_OR_BINDING_CHANGED")
        if action == "APPROVE" and confirm_source_support is not True:
            raise ValueError("EXPLICIT_SOURCE_SUPPORT_ATTESTATION_REQUIRED")
        key = digest([binding_id, action, reviewer, reason, expected_revision])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM event_decisions WHERE binding_id=? ORDER BY id DESC LIMIT 1", (binding_id,)).fetchone()
            if old and old["decision_key"] == key:
                return dict(old)
            if (old["id"] if old else 0) != expected_revision:
                raise ValueError("REVIEW_REVISION_CONFLICT")
            db.execute("INSERT INTO event_decisions(binding_id, request_key, claim_id, action, recorded_at, reviewer, reason, decision_key) VALUES(?,?,?,?,?,?,?,?)",
                       (binding_id, request_key, claim_id, action, timestamp(now or datetime.now(timezone.utc)), reviewer, reason, key))
            result = db.execute("SELECT * FROM event_decisions WHERE decision_key=?", (key,)).fetchone()
        return dict(result)


def reviewed_report(store, reviews, request_key):
    result = call_report(store, request_key)
    for claim in result["claims"]:
        decision = reviews.latest(claim["binding_id"])
        claim["decision"] = decision
        claim["review_revision"] = decision["id"] if decision else 0
        claim["review_status"] = decision["action"] if decision else "PENDING"
        claim["approved_for_research_report"] = bool(decision and decision["action"] == "APPROVE")
    return result


def render_reviewed(result):
    def plain(value):
        return " ".join(str(value).split()).replace("@", "[at]").replace("<", "(").replace(">", ")")
    lines = [f"{plain(result.get('ticker', 'EP'))} | 公告解读审核", f"请求：{result['request_key']}",
             "依据归档原文的 AI 提案，不代表已验证催化或买入信号；发送状态见独立 outbox。"]
    for claim in result["claims"]:
        lines.extend(["", f"[{claim['review_status']}] {plain(claim['text'])}",
                      f"claim_id={claim['claim_id']}", f"binding_id={claim['binding_id']}",
                      f"review_revision={claim['review_revision']}"])
        for evidence in claim["evidence"]:
            lines.extend([f"引文 {evidence['paragraph_id']}：{plain(evidence['quote'])}",
                          f"完整上下文：{plain(evidence['full_paragraph'])}"])
    for item in result["rejected"]:
        lines.append("[BLOCKED] " + plain(item.get('note', {}).get('text', item.get('disclosure_id', ''))) + " | " + ", ".join(item["reasons"]))
    if result.get('sections'):
        lines.append('程序比较：' + ', '.join(result['sections']['comparison_blockers']))
    if result.get("source_url"):
        lines.append("来源：" + result["source_url"])
    return "\n".join(lines)
