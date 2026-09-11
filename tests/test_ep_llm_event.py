from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest

from src.breakouts.ep.llm_event import prepare_event_packet, validate_event
from src.breakouts.ep.llm_service import plan_llm, run_llm
from src.breakouts.ep.llm_provider import responses_payload
from test_ep_llm import KIMI, CLOCK, seeded, evidence_source, KimiTransport
from test_ep_llm_trial import trial
from test_ep_sources import observed

OPTIONS = {"protocol": "event-interpretation", "paragraph_ids": ["p0002"]}


def test_real_event_responses_replay_with_explicit_semantic_limits():
    fixture = json.loads((Path(__file__).parent / "fixtures" / "ep_event_live_20260910.json").read_text())
    results = {}
    for row in fixture["records"]:
        result = validate_event(row["packet"], row["response"])
        assert result == row["expected_validation"]
        assert not result["semantic_support_verified"] and not result["eligible_for_rating"]
        assert result["delivery"] == "DISABLED_SHADOW_ONLY"
        assert all(note["review_required"] for note in result["accepted"])
        results[row["ticker"]] = result
    assert (len(results["PLAB"]["accepted"]), len(results["PLAB"]["rejected"])) == (1, 2)
    assert (len(results["NYAX"]["accepted"]), len(results["NYAX"]["rejected"])) == (3, 0)
    # Characterize v1's known gaps, not a guarantee of semantic correctness.
    assert "p0014" not in results["PLAB"]["accepted"][0]["paragraph_ids"]
    assert "FPD下滑" in results["PLAB"]["accepted"][0]["text"]
    assert any("超过二十万" in note["text"] for note in results["NYAX"]["accepted"])
    assert any("二零二六年第四季度" in note["text"] for note in results["NYAX"]["accepted"])


def answer(request, text="公司发布了财务结果，原文同时列示不同会计口径。", ids=None):
    return {**{k: request[k] for k in ("request_id", "document_id", "text_revision")},
            "scope_status": "COMPLETE_FOR_INPUT", "notes": [{"kind": "EVENT", "text": text,
            "paragraph_ids": ids or [request["untrusted_blocks"][0]["paragraph_id"]]}]}


class EventTransport(KimiTransport):
    def generate(self, payload):
        self.payloads.append(payload)
        request = json.loads(payload["messages"][1]["content"])
        return {"object": "chat.completion", "model": "kimi-k2.6", "choices": [{"message": {"role": "assistant", "content": json.dumps(answer(request))},
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 1000, "completion_tokens": 200}}


def test_scope_citations_and_unverified_semantics():
    packet = prepare_event_packet(evidence_source(), ["p0002"])
    before = deepcopy(packet)
    result = validate_event(packet, answer(packet["request"]))
    assert packet == before
    assert len(result["accepted"]) == 1 and not result["semantic_support_verified"]
    assert result["accepted"][0]["evidence"][0]["quote"] == packet["request"]["untrusted_blocks"][0]["text"]
    assert not result["eligible_for_rating"] and result["external_requests"] == 0
    payload = responses_payload(packet["request"], KIMI)
    assert "notes" in payload["response_format"]["json_schema"]["schema"]["properties"]
    assert "event_packet" not in payload["messages"][1]["content"]


@pytest.mark.parametrize("text,ids,reason", [
    ("公司营收增长20%。", None, "NUMERIC_CLAIM_OUTSIDE_EVENT_SCOPE"),
    ("公司发布最新财报。", None, "RESTRICTED_ASSERTION_REQUIRES_SEPARATE_REVIEW"),
    ("公司发布公告。", ["missing"], "UNKNOWN_EVENT_CITATION"),
    ("公司发布公告。", ["p0002", "p0002"], "DUPLICATE_EVENT_CITATION"),
])
def test_rejects_known_scope_and_citation_violations(text, ids, reason):
    packet = prepare_event_packet(evidence_source(), ["p0002"])
    result = validate_event(packet, answer(packet["request"], text, ids))
    assert reason in result["rejected"][0]["reasons"] and not result["accepted"]


def test_envelope_source_and_version_guards():
    packet = prepare_event_packet(evidence_source(), ["p0002"])
    raw = answer(packet["request"])
    raw["extra"] = "not allowed"
    with pytest.raises(ValueError, match="INVALID_EVENT_RESPONSE"):
        validate_event(packet, raw)
    raw.pop("extra")
    raw["document_id"] = "different"
    with pytest.raises(ValueError, match="EVENT_DOCUMENT_VERSION"):
        validate_event(packet, raw)
    packet["request"]["title"] = "changed"
    with pytest.raises(ValueError, match="LOCAL_PACKET_CHANGED"):
        validate_event(packet, raw)
    for ids in ([], ["missing"], ["p0002"] * 2):
        with pytest.raises(ValueError):
            prepare_event_packet(evidence_source(), ids)


def test_shared_budget_dedup_raw_response_and_readonly_plan(observed):
    store, _, sid = seeded(observed)
    run_llm(store, sid, KIMI, KimiTransport(), clock=CLOCK)
    before = deepcopy(store.llm_history(sid))
    plan = plan_llm(store, sid, KIMI, as_of=CLOCK(), **OPTIONS)
    assert plan["preflight"]["status"] == "READY" and store.llm_history(sid) == before
    limited = replace(KIMI, total_microusd=sum(r["reserved_microusd"] for r in before))
    fake = EventTransport()
    assert run_llm(store, sid, limited, fake, clock=CLOCK, **OPTIONS)["status"] == "BUDGET_EXHAUSTED"
    assert not fake.payloads
    done = run_llm(store, sid, KIMI, fake, clock=CLOCK, expected_request_key=plan["request_key"], **OPTIONS)
    assert done["status"] == "VALIDATED" and len(fake.payloads) == 1
    repeated = run_llm(store, sid, KIMI, fake, clock=CLOCK, **OPTIONS)
    assert repeated["external_requests"] == 0 and len(fake.payloads) == 1
    with store.connection() as db:
        row = db.execute("SELECT request_json,response_json FROM ep_llm_calls WHERE request_key=?", (done["request_key"],)).fetchone()
    assert "event_packet" in json.loads(row[0]) and json.loads(row[1])["choices"]


def test_event_cli_plan_and_wrong_key_do_not_read_secret(trial, observed, monkeypatch, capsys):
    store, _, sid = seeded(observed)
    monkeypatch.setattr(trial, "DATABASE", store.path)
    monkeypatch.setattr(trial, "SOURCES", {"PLAB": sid})
    monkeypatch.setattr(trial, "read_key", lambda *a: pytest.fail("unexpected key read"))
    argv = ["run.py", "--provider", "kimi-cn", "event-plan", "PLAB", "--paragraph-ids", "p0002"]
    monkeypatch.setattr(sys, "argv", argv)
    assert trial.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["http_requests"] == 0 and result["budget"]["calls"] == 0
    argv[3] = "event-run"
    monkeypatch.setattr(sys, "argv", argv + ["--expected-request-key", "0" * 64, "--execute"])
    with pytest.raises(ValueError, match="PLANNED_REQUEST_CHANGED"):
        trial.main()
