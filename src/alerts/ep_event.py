"""Independent, durable delivery of explicitly unverified AI disclosure commentary."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

import requests

from src.alerts.discord import DiscordDeliveryError, DiscordNotifier, validate_discord_payload
from src.breakouts.ep.models import digest, encode
from src.breakouts.ep.catalyst import current_commentary_window


def _plain(text):
    text = " ".join(str(text).split()).replace("@", "[at]")
    return re.sub(r"([\\`*_{}\[\]()<>#|~])", r"\\\1", text)


def ai_payload(report, *, style='annotated'):
    if style not in {'annotated', 'personal'}:
        raise ValueError('UNKNOWN_COMMENTARY_STYLE')
    if report.get("security_eligible") is not True:
        raise ValueError("FRESH_STOCK_OR_ADR_IDENTITY_REQUIRED")
    if not report.get("claims"):
        raise ValueError("NO_VALIDATED_PROPOSALS")
    freshness = report.get("freshness", {})
    boundary = (freshness.get("status") == "BOUNDARY_RELEASE_TIME_UNVERIFIED"
                and freshness.get("provider_time_in_window") is True)
    if not current_commentary_window(freshness):
        raise ValueError("NOT_CURRENT_DISCLOSURE_WINDOW")
    date = report["freshness"].get("announcement", {}).get("date", "日期未核准")
    embeds, included, used = [], [], 0
    claims = report['claims']
    if style == 'personal':
        claims = sorted(claims, key=lambda c: c.get('kind') == 'SOURCE_DISCLOSURE')
    for claim in claims:
        if claim.get("review_status") in {"REJECT", "REVOKE"}:
            continue
        title = _plain(report["ticker"]) + (" | 公告财务原文摘录" if claim.get('kind') == 'SOURCE_DISCLOSURE'
                                          else " | 事件解读" if style == 'personal' else " | 未核准的 AI 解读")
        lead = ('以下为公告原文，未进行跨期间或会计口径换算。'
                if style == 'personal' and claim.get('kind') == 'SOURCE_DISCLOSURE' else claim['text'])
        description = _plain(lead) + "\n\n" + "\n\n".join(
            f"原文 {e['paragraph_id']}：{_plain(e['quote'])}" for e in claim["evidence"])
        footer = f"公告日期 {date}；精确发布时间、价格因果和交易触发未核准。"
        if style == 'personal':
            footer = f"公告日期 {date}；不代表已确认价格因果、量能或开盘突破。"
        if boundary:
            footer += "隔夜候选仅由供应商时间定位，公告实际发布时间待核实。"
        size = len(title) + len(description) + len(footer)
        if len(description) > 4096 or used + size > 5400:
            continue  # Never silently truncate a quote or remove a condition.
        embeds.append({"title": title, "description": description, "url": report["source_url"],
                       "footer": {"text": footer}, "color": 0x757575})
        included.append(claim["claim_id"])
        used += size
    if not embeds:
        raise ValueError("NO_SENDABLE_PROPOSALS")
    comparisons = report.get('sections', {}).get('program_comparisons', [])
    compared = 0
    for comparison in comparisons:
        if comparison.get('status') != 'COMPUTED' or not report.get('finance_input_revision'):
            continue
        label = '实际值与公告前一致预期' if comparison['relation'] == 'SURPRISE' else '本次指引与先前指引'
        description = (f"{label}：{comparison['left_value']} / {comparison['right_value']}"
            f"\n{comparison['metric']}；{comparison['period']}；{comparison['basis']}；{comparison['currency']}"
            f"\n差额：{comparison['difference']}；变化百分比：{comparison.get('percent') or '不适用'}"
            '\n证据：' + ', '.join(comparison['evidence_ids']))
        description = _plain(description)
        if used + len(description) + 60 > 5400 or len(embeds) >= 9:
            continue
        embeds.append({'title': _plain(report['ticker']) + ' | 程序财务比较', 'description': description,
                       'footer': {'text': '基于绑定输入计算，不是模型生成数字，也不是买入信号。'}})
        used += len(description) + 60
        compared += 1
    payload = {"username": "EP AI Research", "allowed_mentions": {"parse": []},
               "content": "未经人工核准的 AI 公告解读。引文位置匹配不等于结论已验证；不是评级、突破确认或买入信号。"
                          + f"\n本条展示 {len(included)}/{len(report['claims'])} 个提案；不代表完整公告覆盖。",
               "embeds": embeds}
    if report.get('protocol') == 'event-context':
        payload['content'] += '\n财务数字为原文摘录；程序比较：缺少核准的可比预期，未计算 beat 或指引变化。AI 解读独立标注。'
    if style == 'personal':
        payload['content'] = ('公告事件摘要，不是评级、突破确认或买入信号。'
            + f"\n本条展示 {len(included)}/{len(report['claims'])} 条内容，不代表完整公告覆盖。")
        if report.get('protocol') == 'event-context':
            payload['content'] += '\n财务数字见原文；缺少已对齐的公告前比较基准，未计算超预期幅度或指引变化。'
    if comparisons:
        payload['content'] = payload['content'].split('\n财务数字')[0] + (
            f'\n独立程序比较展示 {compared}/{len(comparisons)} 项；其余指标不据此推断超预期或指引变化。')
    return validate_discord_payload(payload), included


def webhook_channel(webhook):
    parts = urlsplit(webhook)
    if (parts.query or parts.fragment or parts.username or parts.password or parts.port not in {None, 443}
            or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9_.-]+", parts.path)):
        raise ValueError("DISCORD_DIRECT_CHANNEL_WEBHOOK_REQUIRED")
    DiscordNotifier(webhook, timeout=15, max_rate_limit_retries=0)
    try:
        response = requests.get(webhook, timeout=10, allow_redirects=False)
        channel = str(response.json().get("channel_id")) if response.status_code == 200 else None
    except (requests.RequestException, ValueError, AttributeError):
        raise ValueError("DISCORD_CHANNEL_VERIFICATION_FAILED") from None
    if channel is None or not re.fullmatch(r"[0-9]{1,30}", channel):
        raise ValueError("DISCORD_CHANNEL_VERIFICATION_FAILED")
    return channel


class VerifiedNotifier:
    def __init__(self, webhook, channel_id):
        if not re.fullmatch(r"[0-9]{1,30}", channel_id):
            raise ValueError("EXPECTED_CHANNEL_REQUIRED")
        if webhook_channel(webhook) != channel_id:
            raise ValueError("DISCORD_CHANNEL_MISMATCH")
        self.notifier = DiscordNotifier(webhook, timeout=15, max_rate_limit_retries=0)

    def send(self, payload):
        return self.notifier.send(payload)


class EventOutbox:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables and "ep_ai_outbox" not in tables:
                raise ValueError("NOT_AN_EP_OUTBOX")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS ep_ai_outbox(
                    id TEXT PRIMARY KEY, ticker TEXT NOT NULL, request_key TEXT NOT NULL,
                    route TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    updated REAL NOT NULL, next_attempt REAL NOT NULL, message_id TEXT, error TEXT);
                CREATE INDEX IF NOT EXISTS ep_ai_outbox_due ON ep_ai_outbox(state, next_attempt);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def enqueue(self, report, route, *, now=None, style='annotated'):
        now = time.time() if now is None else now
        payload, _ = ai_payload(report, style=style)
        identity = ['UNVERIFIED_AI_DISCLOSURE_V1', report['document_id'], report['text_revision']]
        if report.get('sections', {}).get('program_comparisons'):
            identity.append(report['finance_input_revision'])
        key = digest(identity)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO ep_ai_outbox VALUES(?,?,?,?,?,?,?,'PENDING',0,?,?,NULL,NULL)",
                       (key, report["ticker"], report["request_key"], route, encode(payload), now, now + 5400, now, now))
        return key

    def status(self):
        with self.connect() as db:
            return dict(db.execute("SELECT state, COUNT(*) FROM ep_ai_outbox GROUP BY state").fetchall())

    def deliver(self, sender, route, *, limit=2, now=None, report_loader=None, style='annotated'):
        now = time.time() if now is None else now
        if not 1 <= limit <= 2 or report_loader is None:
            raise ValueError("BOUNDED_DELIVERY_AND_FRESH_REPORT_REQUIRED")
        outcomes = []
        for _ in range(limit):
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE ep_ai_outbox SET state='UNKNOWN', error='INTERRUPTED_SEND' WHERE state='SENDING' AND updated<?", (now - 120,))
                db.execute("UPDATE ep_ai_outbox SET state='EXPIRED' WHERE state IN ('PENDING','RETRY') AND expires<=?", (now,))
                row = db.execute("""SELECT * FROM ep_ai_outbox q WHERE state IN ('PENDING','RETRY') AND next_attempt<=?
                    AND NOT EXISTS(SELECT 1 FROM ep_ai_outbox p WHERE p.ticker=q.ticker AND p.state IN ('SENT','SENDING','UNKNOWN') AND p.id!=q.id AND p.updated>?)
                    ORDER BY created, id LIMIT 1""", (now, now - 3600)).fetchone()
                if row is None:
                    break
                if row["route"] != route:
                    db.execute("UPDATE ep_ai_outbox SET state='HELD', error='ROUTE_CHANGED' WHERE id=?", (row["id"],))
                    continue
                db.execute("UPDATE ep_ai_outbox SET state='SENDING', attempts=attempts+1, updated=? WHERE id=?", (now, row["id"]))
            state, error, message_id, next_attempt = "UNKNOWN", None, None, now
            try:
                current, _ = ai_payload(report_loader(row["request_key"]), style=style)
                if encode(current) != row["payload"]:
                    raise ValueError("PAYLOAD_OR_REVIEW_CHANGED")
                result = sender.send(json.loads(row["payload"]))
                if not re.fullmatch(r"[0-9]{1,30}", str(result.get("message_id", ""))):
                    raise RuntimeError("MESSAGE_ID_UNCONFIRMED")
                state, message_id = "SENT", str(result["message_id"])
            except ValueError:
                state, error = "HELD", "SOURCE_FRESHNESS_PAYLOAD_OR_REVIEW_CHANGED"
            except DiscordDeliveryError as exc:
                if exc.uncertain:
                    state = "UNKNOWN"
                elif exc.retryable and row["attempts"] < 2:
                    state, next_attempt = "RETRY", now + max(30, min(exc.retry_after or 30, 3600))
                else:
                    state = "FAILED"
                error = "DISCORD_DELIVERY_" + state
            except Exception:
                state, error = "UNKNOWN", "DELIVERY_OUTCOME_UNCERTAIN"
            with self.connect() as db:
                db.execute("UPDATE ep_ai_outbox SET state=?, updated=?, next_attempt=?, message_id=?, error=? WHERE id=? AND state='SENDING'",
                           (state, now, next_attempt, message_id, error, row["id"]))
            outcomes.append({"id": row["id"], "state": state, "message_id": message_id})
        return outcomes


def notifications(config, result, *, sender_factory=None):
    if not config.delivery_enabled:
        return {"status": "DISABLED", "external_requests": 0}
    from src.breakouts.ep.event_worker import private_text
    from src.breakouts.ep.event_worker import financial_report
    from src.breakouts.ep.event_workflow import EventReviewStore, reviewed_report
    from src.breakouts.ep.store import EpStore
    sender_factory = sender_factory or VerifiedNotifier
    store = EpStore(config.database, read_only=True)
    reviews = EventReviewStore(config.reviews_database)
    def load_report(key):
        return financial_report(store, reviewed_report(store, reviews, key), config, datetime.now(timezone.utc))
    outbox = EventOutbox(config.outbox_database)
    route = digest(["discord-channel", config.expected_channel_id])
    style = getattr(config, 'commentary_style', 'annotated')
    held = []
    if config.collect_enabled or config.queue_database:
        from src.breakouts.ep.pipeline import queue_path
        from src.breakouts.ep.queue import PipelineQueue
        if Path(queue_path(config)).is_file():
            queue = PipelineQueue(queue_path(config))
            queue.recover_delivery_handoffs(datetime.now(timezone.utc))
            if config.financial_input_path:
                # New bound inputs can enrich an already analyzed event without
                # another model call. Each immutable input revision gets one handoff.
                with queue.connection() as db:
                    analyses = [dict(r) for r in db.execute('''SELECT * FROM jobs
                        WHERE stage='ANALYSIS' AND state='COMPLETE' AND expires_at>?
                        ORDER BY updated_at DESC LIMIT 20''', (datetime.now(timezone.utc).isoformat(),))]
                for analysis in analyses:
                    key = json.loads(analysis['result'] or '{}').get('request_key')
                    if not key:
                        continue
                    try:
                        updated = load_report(key)
                        if updated.get('sections', {}).get('program_comparisons'):
                            queue.enqueue('DELIVERY', analysis['ticker'], analysis['document_id'],
                                analysis['revision_id'] + ':finance:' + updated['finance_input_revision'],
                                {'request_key': key}, datetime.now(timezone.utc), datetime.fromisoformat(analysis['expires_at']))
                    except (ValueError, KeyError):
                        continue
            for _ in range(20):
                job = queue.claim('DELIVERY', datetime.now(timezone.utc))
                if job is None:
                    break
                try:
                    report = load_report(job['payload']['request_key'])
                    outbox_id = outbox.enqueue(report, route, style=style)
                    queue.finish(job, 'COMPLETE', 'PERSISTED_TO_DISCORD_OUTBOX_NOT_YET_SENT', datetime.now(timezone.utc),
                                 result={'outbox_id': outbox_id})
                except (ValueError, KeyError):
                    queue.finish(job, 'BLOCKED', 'NO_SENDABLE_CURRENT_DISCLOSURE', datetime.now(timezone.utc))
                    held.append({'job_id': job['job_id'], 'reason': 'NO_SENDABLE_CURRENT_DISCLOSURE'})
    for item in result["items"]:
        if "report" not in item:
            continue
        try:
            report = load_report(item['report']['request_key'])
            outbox.enqueue(report, route, style=style)
        except ValueError:
            held.append({"source_id": item["source_id"], "reason": "NO_SENDABLE_CURRENT_DISCLOSURE"})
    counts = outbox.status()
    if not counts.get("PENDING", 0) and not counts.get("RETRY", 0) and not counts.get("SENDING", 0):
        return {"status": "NO_PENDING_MESSAGES", "outbox": counts, "held": held, "external_requests": 0}
    sender = sender_factory(private_text(config.webhook_file), config.expected_channel_id)
    sent = outbox.deliver(sender, route, report_loader=load_report, style=style)
    return {"status": "PROCESSED", "outbox": outbox.status(), "outcomes": sent, "held": held}
