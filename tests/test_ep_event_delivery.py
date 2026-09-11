from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from src.alerts.discord import DiscordDeliveryError
from src.alerts.ep_event import EventOutbox, VerifiedNotifier, ai_payload


def report(doc="doc", ticker="TEST"):
    return {"document_id": doc, "text_revision": "revision", "request_key": doc, "ticker": ticker, "security_eligible": True,
            "freshness": {"status": "CURRENT_WINDOW_DATE_ONLY", "announcement": {"date": "2026-09-10"}},
            "source_url": "https://www.sec.gov/Archives/edgar/data/1/000000000000000001/release.htm",
            "claims": [{"claim_id": "claim", "text": "公司拟收购停车技术提供商。", "kind": "FACT",
                        "evidence": [{"paragraph_id": "p1", "quote": "Company entered an agreement to acquire the parking technology provider."}]}]}


class Sender:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def send(self, payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        return {"message_id": "1234567890"}


def test_payload_unverified_no_mentions_no_silent_quote_truncation():
    item = report()
    item["claims"][0]["text"] += "@everyone **not trusted**"
    payload, ids = ai_payload(item)
    assert "未经人工核准" in payload["content"] and "不是评级" in payload["content"]
    assert payload["allowed_mentions"] == {"parse": []}
    assert "@everyone" not in payload["embeds"][0]["description"]
    assert ids == ["claim"]
    item["claims"][0]["evidence"][0]["quote"] = "a" * 5000
    with pytest.raises(ValueError, match="NO_SENDABLE"):
        ai_payload(item)


def test_unknown_security_or_etf_is_not_sendable():
    item = report()
    item["security_eligible"] = False
    with pytest.raises(ValueError, match="STOCK_OR_ADR"):
        ai_payload(item)


@pytest.mark.parametrize("freshness", ["STALE_FOR_CURRENT_EVENT_WINDOW", "ANNOUNCEMENT_DATE_UNVERIFIED", "BOUNDARY_RELEASE_TIME_UNVERIFIED"])
def test_stale_or_uncertain_announcement_is_not_sent(freshness):
    item = report()
    item["freshness"]["status"] = freshness
    with pytest.raises(ValueError, match="NOT_CURRENT"):
        ai_payload(item)


def test_overnight_provider_window_allows_only_explicitly_unverified_commentary():
    item = report()
    item["freshness"].update(status="BOUNDARY_RELEASE_TIME_UNVERIFIED", provider_time_in_window=True)
    payload, _ = ai_payload(item)
    assert "供应商时间" in payload["embeds"][0]["footer"]["text"]
    item["freshness"]["provider_time_in_window"] = False
    with pytest.raises(ValueError, match="NOT_CURRENT"):
        ai_payload(item)


def test_dedup_concurrent_claim_and_cooldown(tmp_path):
    outbox = EventOutbox(tmp_path / "outbox.sqlite3")
    item = report()
    key = outbox.enqueue(item, "route", now=100)
    assert outbox.enqueue(item, "route", now=101) == key
    sender = Sender()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: outbox.deliver(sender, "route", now=102, report_loader=lambda k: item), range(2)))
    assert len(sender.calls) == 1 and outbox.status() == {"SENT": 1}
    second = report("other-doc")
    outbox.enqueue(second, "route", now=103)
    assert outbox.deliver(sender, "route", now=104, report_loader=lambda k: second) == []
    assert outbox.deliver(sender, "route", now=3703, report_loader=lambda k: second)[0]["state"] == "SENT"


def test_ambiguous_timeout_not_automatically_retried(tmp_path):
    outbox = EventOutbox(tmp_path / "outbox.sqlite3")
    item = report()
    outbox.enqueue(item, "route", now=100)
    sender = Sender(DiscordDeliveryError("redacted", uncertain=True))
    result = outbox.deliver(sender, "route", now=101, report_loader=lambda k: item)
    assert result[0]["state"] == "UNKNOWN"
    assert outbox.deliver(sender, "route", now=200, report_loader=lambda k: item) == []
    assert len(sender.calls) == 1


def test_known_rate_limit_retries_are_bounded(tmp_path):
    outbox = EventOutbox(tmp_path / "outbox.sqlite3")
    item = report()
    outbox.enqueue(item, "route", now=100)
    sender = Sender(DiscordDeliveryError("redacted", retryable=True, status_code=429, retry_after=40))
    assert outbox.deliver(sender, "route", now=101, report_loader=lambda k: item)[0]["state"] == "RETRY"
    assert outbox.deliver(sender, "route", now=120, report_loader=lambda k: item) == []
    assert outbox.deliver(sender, "route", now=142, report_loader=lambda k: item)[0]["state"] == "RETRY"
    assert outbox.deliver(sender, "route", now=183, report_loader=lambda k: item)[0]["state"] == "FAILED"
    assert len(sender.calls) == 3


@pytest.mark.parametrize("mode", ["route", "revoked", "expired", "interrupted"])
def test_delivery_rechecks_route_review_ttl_and_interrupted_send(tmp_path, mode):
    outbox = EventOutbox(tmp_path / "outbox.sqlite3")
    item = report()
    key = outbox.enqueue(item, "route", now=100)
    sender = Sender()
    if mode == "revoked":
        item["claims"][0]["review_status"] = "REVOKE"
    if mode == "interrupted":
        with outbox.connect() as db:
            db.execute("UPDATE ep_ai_outbox SET state='SENDING' WHERE id=?", (key,))
    outbox.deliver(sender, "changed" if mode == "route" else "route", now=5600 if mode == "expired" else 300,
                   report_loader=lambda k: item)
    assert not sender.calls
    assert outbox.status() == {"EXPIRED" if mode == "expired" else "UNKNOWN" if mode == "interrupted" else "HELD": 1}


def test_verified_notifier_rejects_wrong_channel_before_post(monkeypatch):
    class Response:
        status_code = 200
        def json(self):
            return {"channel_id": "999"}
    monkeypatch.setattr("src.alerts.ep_event.requests.get", lambda *a, **kw: Response())
    monkeypatch.setattr("src.alerts.ep_event.requests.post", lambda *a, **kw: pytest.fail("wrong channel posted"))
    with pytest.raises(ValueError, match="CHANNEL_MISMATCH"):
        VerifiedNotifier("https://discord.com/api/webhooks/123/test-token", "888")
    with pytest.raises(ValueError, match="DIRECT_CHANNEL"):
        VerifiedNotifier("https://discord.com/api/webhooks/123/test-token?thread_id=888", "999")
