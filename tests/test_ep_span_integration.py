from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sys

import pytest

from src.breakouts.ep.llm_provider import responses_payload
from src.breakouts.ep.llm_service import plan_llm, run_llm
from src.breakouts.ep.llm_span_selection import prepare_span_packet
from src.breakouts.ep.store import EpStore
from test_ep_llm import KIMI, CLOCK, seeded, evidence_source, KimiTransport
from test_ep_llm_trial import trial
from test_ep_sources import observed

SETTINGS = replace(KIMI, max_output_tokens=8000, read_timeout_seconds=180)
OPTIONS = {"protocol": "span-selection", "batch": "eps"}


class SpanTransport(KimiTransport):
    def __init__(self, *, invalid=False, finish="stop"):
        super().__init__()
        self.invalid, self.finish = invalid, finish

    def generate(self, payload):
        self.payloads.append(payload)
        request = json.loads(payload["messages"][1]["content"])
        content = {k: request[k] for k in ("request_id", "document_id", "text_revision")}
        content.update(scope_status="UNCERTAIN", selections=[])
        if self.invalid:
            content["extra"] = "not permitted"
        return {"object": "chat.completion", "model": "kimi-k2.6", "choices": [{"index": 0,
                "message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": self.finish}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}


def test_payload_uses_id_schema_including_nullable_objects():
    packet = prepare_span_packet(evidence_source(), batch="eps")
    payload = responses_payload(packet["request"], SETTINGS)
    schema = payload["response_format"]["json_schema"]["schema"]
    assert "selections" in schema["properties"] and "claims" not in schema["properties"]
    assert "$ref" not in json.dumps(schema) and "anyOf" not in json.dumps(schema)
    subject = schema["properties"]["selections"]["items"]["properties"]["subject_span"]
    assert subject["type"] == ["object", "null"]
    assert subject["required"] == ["start_id", "end_id"]
    assert "validation_request" not in payload["messages"][1]["content"]


def test_dry_run_is_read_only_and_same_key_is_reserved_once(observed):
    store, _, sid = seeded(observed)
    run_llm(store, sid, KIMI, KimiTransport(), clock=CLOCK)
    old = store.llm_history(sid)
    readonly = EpStore(store.path, read_only=True)
    plan = plan_llm(readonly, sid, SETTINGS, as_of=CLOCK(), **OPTIONS)
    assert plan["preflight"]["status"] == "READY" and plan["external_requests"] == 0
    assert store.llm_history(sid) == old
    fake = SpanTransport()
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda _: run_llm(store, sid, SETTINGS, fake, clock=CLOCK,
            expected_request_key=plan["request_key"], **OPTIONS), range(4)))
    assert sum(r["external_requests"] for r in results) == 1 and len(fake.payloads) == 1
    with store.connection() as db:
        row = db.execute("SELECT request_json,response_json,result_json FROM ep_llm_calls WHERE request_key=?", (plan["request_key"],)).fetchone()
    request, response, result = [json.loads(v) for v in row]
    assert request["selection_packet"]["packet_hash"] == plan["packet_hash"]
    assert request["source_request"]["version"] == "ep-span-selection-v1"
    assert json.loads(response["choices"][0]["message"]["content"])["selections"] == []
    assert result["validation"]["external_requests"] == 0  # The validator itself is pure.
    assert result["protocol"] == "span-selection" and result["usage"]["output_tokens"] == 100
    assert store.llm_history(sid)[0] == old[0]
    reused = plan_llm(readonly, sid, SETTINGS, as_of=CLOCK(), **OPTIONS)
    assert reused["preflight"]["existing_request"] and reused["preflight"]["would_reserve_microusd"] == 0


@pytest.mark.parametrize("invalid,finish,error", [
    (True, "stop", "INVALID_SELECTION_RESPONSE"), (False, "length", "LLM_RESPONSE_INCOMPLETE")])
def test_invalid_or_truncated_response_saved_and_charged_not_retried(observed, invalid, finish, error):
    store, _, sid = seeded(observed)
    fake = SpanTransport(invalid=invalid, finish=finish)
    result = run_llm(store, sid, SETTINGS, fake, clock=CLOCK, **OPTIONS)
    assert result["status"] == "FAILED" and result["result"]["error"] == error
    with store.connection() as db:
        row = db.execute("SELECT response_json,reserved_microusd FROM ep_llm_calls").fetchone()
    assert row["response_json"] and row["reserved_microusd"] > 0
    assert result["result"]["usage"] == {"input_tokens": 1000, "output_tokens": 100}
    again = run_llm(store, sid, SETTINGS, fake, clock=CLOCK, **OPTIONS)
    assert again["external_requests"] == 0 and len(fake.payloads) == 1


def test_budget_shared_with_legacy_and_plan_does_not_bypass_atomic_recheck(observed):
    store, _, sid = seeded(observed)
    plan = plan_llm(store, sid, SETTINGS, as_of=CLOCK(), **OPTIONS)
    run_llm(store, sid, KIMI, KimiTransport(), clock=CLOCK)
    used = sum(r["reserved_microusd"] for r in store.llm_history(sid))
    limited = replace(SETTINGS, total_microusd=used)
    assert plan_llm(store, sid, limited, as_of=CLOCK(), **OPTIONS)["preflight"]["status"] == "BUDGET_EXHAUSTED"
    fake = SpanTransport()
    result = run_llm(store, sid, limited, fake, clock=CLOCK, expected_request_key=plan["request_key"], **OPTIONS)
    assert result["status"] == "BUDGET_EXHAUSTED" and not fake.payloads


def test_changed_plan_rejected_before_reservation_or_request(observed):
    store, _, sid = seeded(observed)
    fake = SpanTransport()
    with pytest.raises(ValueError, match="PLANNED_REQUEST_CHANGED"):
        run_llm(store, sid, SETTINGS, fake, clock=CLOCK, expected_request_key="0" * 64, **OPTIONS)
    assert not fake.payloads and not store.llm_history(sid)
    with pytest.raises(ValueError, match="PROTOCOL_OPTIONS"):
        plan_llm(store, sid, SETTINGS, paragraph_ids=["p0002"])


def test_span_cli_plan_never_reads_key_and_execution_requires_matching_key(trial, observed, monkeypatch, capsys):
    store, _, sid = seeded(observed)
    monkeypatch.setattr(trial, "DATABASE", store.path)
    monkeypatch.setattr(trial, "SOURCES", {"SNOW": sid})
    monkeypatch.setattr(trial, "read_key", lambda _: pytest.fail("dry-run read key"))
    paragraphs = store.source_detail(sid)["parsed"]["paragraphs"]
    ids = ",".join(p["id"] for p in paragraphs[:2])
    argv = ["run.py", "--provider", "kimi-cn", "span-plan", "SNOW", "eps", "--paragraph-ids", ids]
    monkeypatch.setattr(sys, "argv", argv)
    assert trial.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["http_requests"] == 0 and result["budget"]["calls"] == 0
    argv[3] = "span-run"
    monkeypatch.setattr(sys, "argv", argv + ["--expected-request-key", "0" * 64, "--execute"])
    with pytest.raises(ValueError, match="PLANNED_REQUEST_CHANGED"):
        trial.main()
    fake = SpanTransport()
    monkeypatch.setattr(trial, "read_key", lambda _: "test-not-a-real-key")
    monkeypatch.setattr("src.breakouts.ep.llm_provider.create_transport", lambda *a: fake)
    monkeypatch.setattr(sys, "argv", argv + ["--expected-request-key", result["plan"]["request_key"], "--execute"])
    assert trial.main() == 0 and len(fake.payloads) == 1
    assert json.loads(capsys.readouterr().out)["protocol"] == "span-selection"
