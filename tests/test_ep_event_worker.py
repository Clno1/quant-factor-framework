from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path

import pytest

from src.breakouts.ep.event_worker import WorkerConfig, cycle, private_key, settings, select_jobs
from src.breakouts.ep.event_workflow import EventReviewStore, reviewed_report, render_reviewed
from src.breakouts.ep.llm_event_claims import prepare_atomic_packet, validate_atomic
from src.breakouts.ep.llm_provider import responses_payload
from src.breakouts.ep.llm_service import plan_llm, run_llm
from test_ep_llm import seeded, CLOCK, evidence_source, KimiTransport
from test_ep_sources import observed

OPTIONS = {"protocol": "event-claims", "paragraph_ids": ["p0002"]}


def response(request, text="公司披露财务结果。", kind="FACT"):
    block = request["untrusted_blocks"][0]
    return {**{k: request[k] for k in ("request_id", "document_id", "text_revision")},
            "scope_status": "UNCERTAIN", "notes": [{"kind": kind, "text": text,
            "paragraph_ids": [block["paragraph_id"]]}]}


class AtomicTransport(KimiTransport):
    def generate(self, payload):
        self.payloads.append(payload)
        request = json.loads(payload["messages"][1]["content"])
        return {"object": "chat.completion", "model": "kimi-k2.6",
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(response(request))}}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 200}}


def config(tmp_path, store, sid, enabled=True):
    return WorkerConfig(database=str(store.path), reviews_database=str(tmp_path / "review.sqlite3"),
                        output_directory=str(tmp_path / "reports"), key_file=str(tmp_path / "key"), enabled=enabled,
                        jobs=[{"source_id": sid, "paragraph_ids": ["p0002"]}])


@pytest.mark.parametrize("text,kind,reason", [
    ("拥有二十万处停车位。", "FACT", "QUANTITY_EXPRESSION_OUTSIDE_EVENT_SCOPE"),
    ("公司披露业绩。已经完成收购。", "FACT", "MULTI_SENTENCE_CLAIM"),
    ("净利润同比增长。", "FACT", "CLAIM_OUTSIDE_QUALITATIVE_SCOPE"),
    ("公司称调整后每股收益超出指引区间", "FACT", "CLAIM_OUTSIDE_QUALITATIVE_SCOPE"),
    ("公司称各指标均达成财年指引", "FACT", "CLAIM_OUTSIDE_QUALITATIVE_SCOPE"),
    ("业务将向欧洲扩张。", "MANAGEMENT_EXPECTATION", "MANAGEMENT_ATTRIBUTION_REQUIRED"),
    ("交易带来交叉销售机会。", "INTERPRETATION", "INTERPRETATION_QUALIFIER_REQUIRED"),
])
def test_atomic_scope_rejections(text, kind, reason):
    packet = prepare_atomic_packet(evidence_source(), ["p0002"])
    result = validate_atomic(packet, response(packet["request"], text, kind))
    assert reason in result["rejected"][0]["reasons"] and not result["accepted"]


def test_exact_quote_context_and_duplicate_checks():
    packet = prepare_atomic_packet(evidence_source(), ["p0002"])
    raw = response(packet["request"])
    result = validate_atomic(packet, raw)
    assert result["accepted"][0]["evidence"][0]["full_paragraph"] == packet["request"]["untrusted_blocks"][0]["text"]
    assert not result["semantic_support_verified"]
    schema = responses_payload(packet["request"], settings())["response_format"]["json_schema"]["schema"]
    assert "paragraph_ids" in schema["properties"]["notes"]["items"]["properties"]
    assert "evidence" not in schema["properties"]["notes"]["items"]["properties"]
    raw["notes"].append(deepcopy(raw["notes"][0]))
    assert "DUPLICATE_CLAIM" in validate_atomic(packet, raw)["rejected"][0]["reasons"]
    raw["notes"] = raw["notes"][:1]
    raw["notes"][0]["paragraph_ids"] = ["invented-paragraph"]
    assert "UNKNOWN_EVENT_CITATION" in validate_atomic(packet, raw)["rejected"][0]["reasons"]
    packet["request"]["title"] = "Changed"
    with pytest.raises(ValueError, match="LOCAL_PACKET_CHANGED"):
        validate_atomic(packet, raw)


def test_worker_readonly_plan_dedup_and_fixed_shared_budget(tmp_path, observed, monkeypatch):
    store, _, sid = seeded(observed)
    job = config(tmp_path, store, sid)
    monkeypatch.setenv("EP_LLM_TOTAL_MICROUSD", "999999999")
    assert settings().total_microusd == 10_000_000
    fake = AtomicTransport()
    kwargs = {"clock": CLOCK, "transport_factory": lambda *a: fake, "key_reader": lambda p: "not-a-real-key"}
    result = cycle(job, key_reader=lambda p: pytest.fail("plan read key"), clock=CLOCK)
    assert result["external_requests"] == 0 and result["budget_before"] == result["budget_after"]
    assert not Path(job.reviews_database).exists()
    done = cycle(job, execute=True, **kwargs)
    assert done["external_requests"] == 1 and len(fake.payloads) == 1
    assert done["items"][0]["report"]["claims"][0]["review_status"] == "PENDING"
    again = cycle(job, execute=True, key_reader=lambda p: pytest.fail("dedup read key"), clock=CLOCK)
    assert again["external_requests"] == 0 and again["budget_before"] == done["budget_after"]


def test_budget_stop_happens_before_key_read(tmp_path, observed):
    store, _, sid = seeded(observed)
    store.reserve_llm_call("older", sid, {}, 10_000_000, replace(settings(True), daily_microusd=10_000_000), CLOCK())
    result = cycle(config(tmp_path, store, sid), execute=True, clock=CLOCK,
                   key_reader=lambda p: pytest.fail("exhausted budget read key"))
    assert result["items"][0]["status"] == "BUDGET_EXHAUSTED"
    assert result["external_requests"] == 0


def test_failed_source_receipts_cannot_crowd_matched_original_out_of_queue(tmp_path, observed, monkeypatch):
    store, report, sid = seeded(observed)
    job = config(tmp_path, store, sid).model_copy(update={"jobs": []})
    event = report["candidates"][0]["events"][0]
    for _ in range(110):
        batch = store.start_source_run(report["run_id"], {}, CLOCK())
        store.save_source_attempt(batch, 'SNOW', event, {"status": "SOURCE_DOCUMENT_BUDGET_EXCEEDED"}, CLOCK())
        store.finish_source_run(batch, {"status": "PARTIAL_SOURCES"}, CLOCK())
    monkeypatch.setattr('src.breakouts.ep.event_worker.source_brief',
                        lambda source, now: {'freshness': {'status': 'CURRENT_WINDOW_DATE_ONLY'}})
    jobs, summary = select_jobs(store, job, CLOCK())
    assert [j.source_id for j in jobs] == [sid]
    assert summary['examined'] == 1
    monkeypatch.setattr('src.breakouts.ep.event_worker.source_brief',
                        lambda source, now: {'freshness': {'status': 'STALE_FOR_CURRENT_EVENT_WINDOW'}})
    jobs, summary = select_jobs(store, job, CLOCK())
    assert jobs == []
    assert summary['skipped'][0]['reason'] == 'CURRENT_COMMENTARY_WINDOW_NOT_ESTABLISHED'


def test_approval_requires_attestation_supports_cas_and_revocation(tmp_path, observed):
    store, _, sid = seeded(observed)
    done = run_llm(store, sid, settings(True), AtomicTransport(), clock=CLOCK, **OPTIONS)
    reviews = EventReviewStore(tmp_path / "reviews.sqlite3", read_only=False)
    key = done["request_key"]
    item = reviewed_report(store, reviews, key)["claims"][0]
    common = {"binding_id": item["binding_id"], "reviewer": "test-reviewer", "reason": "Read source context",
              "expected_revision": 0, "now": CLOCK()}
    with pytest.raises(ValueError, match="ATTESTATION"):
        reviews.decide(store, key, item["claim_id"], action="APPROVE", **common)
    approved = reviews.decide(store, key, item["claim_id"], action="APPROVE", confirm_source_support=True, **common)
    assert reviews.decide(store, key, item["claim_id"], action="APPROVE", confirm_source_support=True, **common) == approved
    report = reviewed_report(store, reviews, key)
    assert report["claims"][0]["approved_for_research_report"]
    assert "[APPROVE]" in render_reviewed(report)
    with pytest.raises(ValueError, match="REVISION_CONFLICT"):
        reviews.decide(store, key, item["claim_id"], action="REVOKE", **common)
    common["expected_revision"] = approved["id"]
    revoked = reviews.decide(store, key, item["claim_id"], action="REVOKE", **common)
    assert revoked["id"] > approved["id"]
    assert not reviewed_report(store, reviews, key)["claims"][0]["approved_for_research_report"]


def test_changed_source_and_hard_rejection_cannot_be_approved(tmp_path, observed, monkeypatch):
    store, _, sid = seeded(observed)
    done = run_llm(store, sid, settings(True), AtomicTransport(), clock=CLOCK, **OPTIONS)
    reviews = EventReviewStore(tmp_path / "reviews.sqlite3", read_only=False)
    key = done["request_key"]
    item = reviewed_report(store, reviews, key)["claims"][0]
    with pytest.raises(ValueError, match="CLAIM_BLOCKED"):
        reviews.decide(store, key, "not-a-valid-claim", binding_id=item["binding_id"], action="APPROVE",
                       reviewer="test", reason="test", expected_revision=0, confirm_source_support=True)
    source = store.source_detail(sid)
    source["parsed"]["text_revision"] = "different"
    monkeypatch.setattr(store, "source_detail", lambda *a, **kw: source)
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        reviewed_report(store, reviews, key)


def test_concurrent_requests_only_reserve_once(observed):
    store, _, sid = seeded(observed)
    fake = AtomicTransport()
    def invoke(_):
        return run_llm(store, sid, settings(True), fake, clock=CLOCK, **OPTIONS)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(invoke, range(2)))
    assert sum(row["external_requests"] for row in result) == 1
    assert len(fake.payloads) == 1


def test_existing_reserved_call_not_automatically_retried(tmp_path, observed):
    store, _, sid = seeded(observed)
    plan = plan_llm(store, sid, settings(True), as_of=CLOCK(), **OPTIONS)
    store.reserve_llm_call(plan["request_key"], sid, {}, plan["budget_estimate"]["reserved_microusd"], settings(True), CLOCK())
    result = cycle(config(tmp_path, store, sid), execute=True, clock=CLOCK,
                   key_reader=lambda p: pytest.fail("orphan reservation read key"))
    assert result["items"][0]["status"] == "RESERVED" and result["external_requests"] == 0


def test_private_key_permissions_and_symlink(tmp_path):
    key = tmp_path / "key"
    key.write_text("a" * 32)
    key.chmod(0o600)
    assert private_key(key) == "a" * 32
    link = tmp_path / "symlink"
    link.symlink_to(key)
    with pytest.raises(OSError):
        private_key(link)
    key.chmod(0o644)
    with pytest.raises(ValueError, match="OWNER_OR_MODE"):
        private_key(key)


def test_review_database_cannot_replace_budget_journal(observed):
    store, _, _ = seeded(observed)
    with pytest.raises(ValueError, match="NOT_AN_EVENT_REVIEW_DATABASE"):
        EventReviewStore(store.path, read_only=False)
