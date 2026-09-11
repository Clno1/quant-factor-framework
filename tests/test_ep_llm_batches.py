from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import sys

import pytest

from src.breakouts.ep.llm_batches import SCOPES, format_examples
from src.breakouts.ep.llm_contract import prepare_request, validate_response, response_schema
from src.breakouts.ep.llm_provider import responses_payload
from src.breakouts.ep.llm_service import run_llm, plan_llm, batch_status
from test_ep_llm import KIMI, CLOCK, seeded, evidence_source, claim as legacy_claim, envelope, KimiTransport
from test_ep_sources import observed
from test_ep_llm_trial import trial

BATCH_SETTINGS = replace(KIMI, max_output_tokens=8000, read_timeout_seconds=180)


def claim(**changes):
    value = legacy_claim(**changes)
    value.update(unit_scale_text=None, context_evidence=[])
    for role, field in (("SUBJECT", "subject_text"), ("PERIOD", "period_text"), ("CURRENCY", "unit_text")):
        for citation in value["evidence"]:
            if value[field] and value[field] in citation["quote"]:
                value["context_evidence"].append({"role": role, "text": value[field], **citation})
                break
    return value


class BatchTransport(KimiTransport):
    def __init__(self, claims=None, scope_status="UNCERTAIN"):
        super().__init__()
        self.claims = claims or []
        self.scope_status = scope_status

    def generate(self, payload):
        self.payloads.append(payload)
        request = json.loads(payload["messages"][1]["content"])
        content = envelope(request, self.claims) | {"scope_status": self.scope_status}
        return {"object": "chat.completion", "model": "kimi-k2.6", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": json.dumps(content)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000}}


def test_scopes_have_stable_distinct_ids_and_keep_all_input_context():
    source = evidence_source()
    before = deepcopy(source)
    original = prepare_request(source)
    requests = [prepare_request(source, batch=name) for name in SCOPES]
    assert len({r["request_id"] for r in requests} | {original["request_id"]}) == 5
    assert original == prepare_request(source)
    assert source == before
    for request in requests:
        assert request["untrusted_paragraphs"] == original["untrusted_paragraphs"]
        assert request["coverage"] == original["coverage"]
        assert request["batch"]["claim_limit"] == 4
        assert request == prepare_request(source, batch=request["batch"]["name"])
    with pytest.raises(ValueError, match="UNKNOWN_LLM_BATCH"):
        prepare_request(source, batch="random-to-bypass-dedup")


@pytest.mark.parametrize("batch", list(SCOPES))
def test_wire_schema_bounds_match_local_contract(batch):
    request = prepare_request(evidence_source(), batch=batch)
    schema = response_schema(request)
    assert schema["properties"]["claims"]["maxItems"] == 4
    assert schema["$defs"]["Citation"]["properties"]["quote"]["maxLength"] == 1200
    assert schema["$defs"]["Claim"]["properties"]["evidence"]["maxItems"] == 4
    payload = responses_payload(request, BATCH_SETTINGS)
    wire = payload["response_format"]["json_schema"]["schema"]
    assert "$ref" not in json.dumps(wire)
    assert payload["max_completion_tokens"] == 8000
    assert wire["properties"]["claims"]["items"]["properties"]["metric"]["enum"] == request["batch"]["metrics"]
    with pytest.raises(ValueError, match="OUTPUT_LIMIT_TOO_SMALL"):
        responses_payload(request, KIMI)


def test_batch_validation_keeps_strict_evidence_and_checks_scope():
    request = prepare_request(evidence_source(), batch="eps")
    raw = envelope(request, [claim()]) | {"scope_status": "COMPLETE_FOR_SCOPE"}
    result = validate_response(request, raw)
    assert len(result["accepted"]) == 1 and not result["rejected"]
    assert result["exhaustiveness_verified"] is False
    assert result["accepted"][0]["financial_semantics_verified"] is False
    raw["claims"][0]["unit_text"] = "USD"
    assert "UNIT_TEXT_NOT_GROUNDED" in validate_response(request, raw)["rejected"][0]["reasons"]
    raw["claims"][0] = claim(value_kind="COMPANY_GUIDANCE")
    assert "CLAIM_OUTSIDE_BATCH_SCOPE" in validate_response(request, raw)["rejected"][0]["reasons"]
    raw["claims"][0] = claim(evidence=claim()["evidence"] * 5)
    assert "BATCH_EVIDENCE_LIMIT_EXCEEDED" in validate_response(request, raw)["rejected"][0]["reasons"]


def test_format_examples_pass_the_same_validator_and_cannot_leak_into_source():
    examples = format_examples()
    for name, proposal in zip(("revenue", "eps"), examples["valid_claims"]):
        request = prepare_request(evidence_source(), batch=name)
        raw = envelope(request, [proposal]) | {"scope_status": "UNCERTAIN"}
        assert not validate_response(request, raw)["accepted"]
        request["untrusted_paragraphs"] = examples["paragraphs"]
        validated = validate_response(request, raw)
        assert len(validated["accepted"]) == 1 and not validated["rejected"]
        assert validated["accepted"][0]["financial_semantics_verified"] is False


def test_claim_cap_and_scope_status_cannot_be_ignored():
    request = prepare_request(evidence_source(), batch="eps")
    raw = envelope(request, [claim()] * 5) | {"scope_status": "COMPLETE_FOR_SCOPE"}
    with pytest.raises(ValueError, match="CLAIM_COUNT"):
        validate_response(request, raw)
    raw["claims"] = raw["claims"][:4]
    assert validate_response(request, raw)["claim_limit_reached"] is True
    raw["scope_status"] = "EVERYTHING_VERIFIED"
    with pytest.raises(ValueError, match="SCOPE_STATUS"):
        validate_response(request, raw)
    del raw["scope_status"]
    with pytest.raises(ValueError, match="ENVELOPE"):
        validate_response(request, raw)


def test_partial_input_never_becomes_complete_via_batching():
    request = prepare_request(evidence_source(), max_paragraphs=1, batch="eps")
    raw = envelope(request, []) | {"scope_status": "COMPLETE_FOR_SCOPE"}
    result = validate_response(request, raw)
    assert result["coverage"]["complete"] is False
    assert result["exhaustiveness_verified"] is False


def test_batches_are_independently_reserved_cached_and_share_old_budget(observed):
    store, _, sid = seeded(observed)
    run_llm(store, sid, KIMI, KimiTransport(), clock=CLOCK)
    old = store.llm_history(sid)
    fake = BatchTransport()
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda _: run_llm(store, sid, BATCH_SETTINGS, fake, batch="revenue", clock=CLOCK), range(4)))
    assert sum(r["external_requests"] for r in results) == 1
    assert len(fake.payloads) == 1 and store.llm_history(sid)[0] == old[0]
    used = sum(row["reserved_microusd"] for row in store.llm_history(sid))
    blocked = run_llm(store, sid, replace(BATCH_SETTINGS, total_microusd=used), fake, batch="eps", clock=CLOCK)
    assert blocked["status"] == "BUDGET_EXHAUSTED" and len(fake.payloads) == 1
    progress = batch_status(store, sid, BATCH_SETTINGS)
    assert progress["batches"]["revenue"]["status"] == "VALIDATED"
    assert progress["batches"]["eps"]["status"] == "NOT_COMPLETED"
    assert progress["all_batches_returned_valid_envelopes"] is False
    assert progress["exhaustiveness_verified"] is False


def test_status_separates_in_progress_and_current_payload(observed):
    store, _, sid = seeded(observed)
    plan = plan_llm(store, sid, BATCH_SETTINGS, batch="eps", as_of=CLOCK())
    store.reserve_llm_call(plan["request_key"], sid, {}, 100, BATCH_SETTINGS, CLOCK())
    assert batch_status(store, sid, BATCH_SETTINGS)["batches"]["eps"]["status"] == "RESERVED"
    assert batch_status(store, sid, replace(BATCH_SETTINGS, max_output_tokens=9000))["batches"]["eps"]["status"] == "NOT_COMPLETED"


@pytest.mark.parametrize("command", ["batch-plan", "batch-status"])
def test_batch_readonly_cli_never_reads_key(trial, observed, monkeypatch, capsys, command):
    store, _, sid = seeded(observed)
    monkeypatch.setattr(trial, "DATABASE", store.path)
    monkeypatch.setattr(trial, "SOURCES", {"SNOW": sid})
    monkeypatch.setattr(trial, "read_key", lambda _: pytest.fail("read-only command read key"))
    monkeypatch.setattr(sys, "argv", ["run.py", "--provider", "kimi-cn", command, "SNOW"])
    assert trial.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["budget"]["calls"] == 0
    assert len(result["batches"]) == 4
    if command == "batch-plan":
        assert all(p["budget_estimate"]["output_token_limit"] == 8000 for p in result["batches"].values())


def test_batch_cli_executes_exactly_one_scope(trial, observed, monkeypatch, capsys):
    store, _, sid = seeded(observed)
    monkeypatch.setattr(trial, "DATABASE", store.path)
    monkeypatch.setattr(trial, "SOURCES", {"SNOW": sid})
    fake = BatchTransport()
    monkeypatch.setattr(trial, "read_key", lambda _: "dummy-test-key")
    monkeypatch.setattr("src.breakouts.ep.llm_provider.create_transport", lambda settings, key: fake)
    argv = ["run.py", "--provider", "kimi-cn", "batch-run", "SNOW", "eps"]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(ValueError, match="EXECUTE_REQUIRED"):
        trial.main()
    monkeypatch.setattr(sys, "argv", argv + ["--execute"])
    assert trial.main() == 0
    assert len(fake.payloads) == 1
    value = json.loads(capsys.readouterr().out)
    assert value["batch"]["name"] == "eps" and value["total_limit_microusd"] == 10000000
