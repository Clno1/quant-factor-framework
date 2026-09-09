from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import os
import subprocess
import sys

import pytest

from src.breakouts.ep.llm_contract import prepare_request, validate_response, strict_json, Response
from src.breakouts.ep.llm_provider import LlmError, LlmSettings, OpenAIResponsesTransport, responses_payload, extract_response
from src.breakouts.ep.llm_service import run_llm, plan_llm
from src.breakouts.ep.store import EpStore
from test_ep_analysis import source
from test_ep_discovery import run, FakeClient, EXHIBIT, release
from test_ep_sources import observed
from test_ep_radar import NOW, ROOT

QUOTE = "Example reported non-GAAP diluted EPS of $0.24 for Q2 FY 2027."


def evidence_source():
    value = source(QUOTE, "Net new ARR growth exceeded 40% year over year.")
    value["result"]["final_url"] = EXHIBIT
    return value


def claim(**changes):
    value = {"metric": "EPS", "metric_text": "EPS", "value_text": "$0.24", "subject_text": "Example",
        "period_text": "Q2 FY 2027", "unit_text": "$", "basis_text": "non-GAAP", "share_basis_text": "diluted",
        "value_kind_text": "reported", "basis": "NON_GAAP", "share_basis": "DILUTED", "value_kind": "ACTUAL",
        "subject_scope": "ISSUER", "evidence": [{"paragraph_id": "p0002", "quote": QUOTE}]}
    value.update(changes)
    return value


def envelope(request, claims):
    return {key: request[key] for key in ("request_id", "document_id", "text_revision")} | {"claims": claims}


def response_body(request, claims=None):
    return {"status": "completed", "model": "gpt-5.4-mini", "output": [{"type": "message", "role": "assistant", "content": [
        {"type": "output_text", "text": json.dumps(envelope(request, claims or []))}]}],
        "usage": {"input_tokens": 1000, "output_tokens": 500}}


class Transport:
    def __init__(self, response=None, error=None):
        self.payloads = []
        self.response = response
        self.error = error

    def generate(self, payload):
        self.payloads.append(payload)
        if self.error:
            raise self.error
        request = json.loads(payload["input"][0]["content"])
        return self.response(request) if self.response else response_body(request)


def seeded(observed):
    store, report = observed
    run(store, report)
    source_id = store.aligned_sources(report["run_id"], "SNOW")[0]["source_id"]
    return store, report, source_id


SETTINGS = LlmSettings(model="gpt-5.4-mini", enabled=True)
CLOCK = lambda: NOW + timedelta(minutes=11)


def test_total_budget_does_not_reset_next_month(observed):
    store, _, source_id = seeded(observed)
    settings = replace(SETTINGS, total_microusd=100)
    assert store.reserve_llm_call("first", source_id, {}, 60, settings, CLOCK())["reserved"]
    blocked = store.reserve_llm_call("second", source_id, {}, 41, settings, CLOCK() + timedelta(days=40))
    assert blocked["status"] == "BUDGET_EXHAUSTED"
    assert not blocked["reserved"]
    assert store.reserve_llm_call("third", source_id, {}, 40, settings, CLOCK() + timedelta(days=40))["reserved"]


def test_total_budget_settings_reject_invalid_values():
    for value in (0, -1, True, "10", 1_000_000_001):
        with pytest.raises(ValueError):
            replace(SETTINGS, total_microusd=value)


def test_accepted_proposals_are_not_semantic_proof_or_signals():
    request = prepare_request(evidence_source())
    result = validate_response(request, envelope(request, [claim(), claim()]))
    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["validation_level"] == "TEXT_GROUNDED_ONLY"
    assert result["accepted"][0]["financial_semantics_verified"] is False
    assert result["accepted"][0]["review_required"]
    assert result["eligible_for_rating"] is False


@pytest.mark.parametrize("change,reason", [
    ({"value_text": "$9.99"}, "VALUE_TEXT_NOT_GROUNDED"),
    ({"period_text": "Q2 FY 2026"}, "PERIOD_TEXT_NOT_GROUNDED"),
    ({"subject_text": "Other Corp"}, "SUBJECT_TEXT_NOT_GROUNDED"),
    ({"basis": "GAAP", "basis_text": "non-GAAP"}, "ACCOUNTING_BASIS_CONTRADICTION"),
    ({"basis": "GAAP", "basis_text": "GAAP"}, "ACCOUNTING_BASIS_CONTEXT_CONTRADICTION"),
    ({"share_basis": "BASIC", "share_basis_text": "diluted"}, "SHARE_BASIS_CONTRADICTION"),
    ({"metric": "REVENUE"}, "METRIC_LABEL_CONTRADICTION"),
    ({"evidence": [{"paragraph_id": "absent", "quote": QUOTE}]}, "QUOTE_NOT_IN_SOURCE"),
    ({"send_discord": True}, "INVALID_CLAIM_SCHEMA"),
])
def test_bad_claims_quarantined_individually(change, reason):
    request = prepare_request(evidence_source())
    result = validate_response(request, envelope(request, [claim(), claim(**change)]))
    assert len(result["accepted"]) == 1
    assert reason in result["rejected"][0]["reasons"]


def test_trimmed_quote_cannot_turn_non_gaap_into_gaap():
    request = prepare_request(evidence_source())
    result = validate_response(request, envelope(request, [claim(basis="GAAP", basis_text="GAAP", subject_text=None,
        evidence=[{"paragraph_id": "p0002", "quote": QUOTE.split("non-")[1]}])]))
    assert not result["accepted"]
    assert "ACCOUNTING_BASIS_FULL_PARAGRAPH_CONTRADICTION" in result["rejected"][0]["reasons"]


def test_numeric_substring_and_version_spoofing_rejected():
    request = prepare_request(evidence_source())
    assert validate_response(request, envelope(request, [claim(value_text="24")]))["rejected"]
    bad = envelope(request, [])
    bad["request_id"] = "wrong"
    with pytest.raises(ValueError, match="VERSION_MISMATCH"):
        validate_response(request, bad)
    bad = envelope(request, []) | {"rating": "Strong"}
    with pytest.raises(ValueError, match="ENVELOPE"):
        validate_response(request, bad)


def test_broader_arr_proposal_with_unresolved_context():
    request = prepare_request(evidence_source())
    raw = claim(metric="ARR", metric_text="Net new ARR", value_text="40%", subject_text=None, period_text=None,
        unit_text="%", basis_text=None, share_basis_text=None, value_kind_text=None, basis="NOT_APPLICABLE",
        share_basis="NOT_APPLICABLE", subject_scope="UNKNOWN", evidence=[{"paragraph_id": "p0003", "quote":
            "Net new ARR growth exceeded 40% year over year."}])
    result = validate_response(request, envelope(request, [raw]))
    assert not result["rejected"]
    assert "PERIOD_TEXT_UNRESOLVED" in result["accepted"][0]["review_required"]


def test_request_limits_source_policy_and_hash_stability():
    src = evidence_source()
    request = prepare_request(src, max_paragraphs=2)
    assert request["coverage"]["complete"] is False
    assert request["coverage"]["included"] == 2
    src["source_id"] = "another-fetch-of-same-content"
    assert prepare_request(src, max_paragraphs=2)["request_id"] == request["request_id"]
    src["result"]["final_url"] = "https://financialmodelingprep.com/article"
    with pytest.raises(ValueError, match="SEC_ORIGINAL"):
        prepare_request(src)
    src = evidence_source()
    src["result"]["issuer_linkage"] = "UNKNOWN"
    with pytest.raises(ValueError, match="ISSUER"):
        prepare_request(src)


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '```json\n{}\n```', 'x' * 1_000_001])
def test_invalid_json_is_not_repaired_or_executed(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


def test_payload_has_strict_schema_no_tools_or_remote_storage():
    request = prepare_request(evidence_source())
    payload = responses_payload(request, SETTINGS)
    assert payload["store"] is False and payload["tools"] == []
    assert payload["text"]["format"]["strict"] is True
    assert payload["text"]["format"]["schema"]["additionalProperties"] is False
    Response.model_validate(envelope(request, [claim()]))
    with pytest.raises(ValueError, match="BUDGET_EXCEEDED"):
        responses_payload(request, replace(SETTINGS, max_request_bytes=1000))


@pytest.mark.parametrize("response,error", [
    ({"status": "incomplete"}, "INCOMPLETE"),
    ({"status": "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "refusal"}]}]}, "REFUSAL"),
    ({"status": "completed", "output": [{"type": "function_call"}]}, "UNEXPECTED"),
])
def test_refusals_truncation_and_tools_are_not_claims(response, error):
    with pytest.raises(LlmError, match=error):
        extract_response(response)


def test_budget_dedup_history_and_candidates_unchanged(observed):
    store, report, source_id = seeded(observed)
    transport = Transport()
    first = run_llm(store, source_id, SETTINGS, transport, clock=CLOCK)
    second = run_llm(store, source_id, SETTINGS, transport, clock=CLOCK)
    assert first["status"] == "VALIDATED" and first["external_requests"] == 1
    assert second["reused"] and second["external_requests"] == 0
    assert len(transport.payloads) == 1
    assert store.report(report["run_id"]) == report
    assert store.review_history(run_id=report["run_id"]) == []
    assert len(store.llm_history(source_id)) == 1
    assert store.llm_history(source_id, as_of=NOW + timedelta(minutes=10)) == []
    limited = replace(SETTINGS, model="gpt-5.4", daily_microusd=1)
    assert run_llm(store, source_id, limited, transport, clock=CLOCK)["status"] == "BUDGET_EXHAUSTED"
    assert len(transport.payloads) == 1


def test_atomic_reservation_allows_one_concurrent_request(observed):
    store, _, source_id = seeded(observed)
    transport = Transport()
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: run_llm(store, source_id, SETTINGS, transport, clock=CLOCK), range(4)))
    assert sum(row["external_requests"] for row in results) == 1
    assert len(transport.payloads) == 1


def test_timeout_is_saved_without_secret_and_never_retried(observed):
    store, _, source_id = seeded(observed)
    transport = Transport(error=LlmError("secret-api-key-should-not-be-recorded"))
    first = run_llm(store, source_id, SETTINGS, transport, clock=CLOCK)
    assert first["status"] == "FAILED"
    assert "secret-api-key" not in json.dumps(store.llm_history(source_id))
    assert run_llm(store, source_id, SETTINGS, transport, clock=CLOCK)["external_requests"] == 0


def test_usage_over_reservation_latches_future_spending(observed):
    store, _, source_id = seeded(observed)
    def huge(request):
        body = response_body(request)
        body["usage"]["output_tokens"] = 100_000_000
        return body
    result = run_llm(store, source_id, SETTINGS, Transport(huge), clock=CLOCK)
    assert result["status"] == "BILLING_REVIEW_REQUIRED"
    transport = Transport()
    assert run_llm(store, source_id, replace(SETTINGS, model="gpt-5.4"), transport, clock=CLOCK)["status"] == "BILLING_REVIEW_REQUIRED"
    assert transport.payloads == []


def test_invalid_response_is_journalled_and_keeps_budget(observed):
    store, _, source_id = seeded(observed)
    transport = Transport(lambda request: {"status": "completed", "n": float("nan")})
    assert run_llm(store, source_id, SETTINGS, transport, clock=CLOCK)["status"] == "FAILED"
    assert store.llm_history(source_id)[0]["reserved_microusd"] > 0


def test_unexpected_model_requires_billing_review(observed):
    store, _, source_id = seeded(observed)
    transport = Transport(lambda request: response_body(request) | {"model": "different-model"})
    assert run_llm(store, source_id, SETTINGS, transport, clock=CLOCK)["status"] == "BILLING_REVIEW_REQUIRED"


def test_monthly_budget_and_unfinished_reservation_do_not_retry(observed):
    store, _, source_id = seeded(observed)
    transport = Transport()
    small = replace(SETTINGS, monthly_microusd=1)
    assert run_llm(store, source_id, small, transport, clock=CLOCK)["status"] == "BUDGET_EXHAUSTED"
    class Crash(BaseException):
        pass
    with pytest.raises(Crash):
        run_llm(store, source_id, SETTINGS, Transport(error=Crash()), clock=CLOCK)
    assert run_llm(store, source_id, SETTINGS, transport, clock=CLOCK)["status"] == "RESERVED"
    assert transport.payloads == []


def test_omitted_paragraph_is_not_available_to_validator():
    request = prepare_request(evidence_source(), max_paragraphs=1)
    assert validate_response(request, envelope(request, [claim()]))["rejected"]


def test_disabled_and_plan_never_call_network_or_write(observed):
    store, _, source_id = seeded(observed)
    transport = Transport()
    with pytest.raises(ValueError, match="DISABLED"):
        run_llm(store, source_id, LlmSettings(), transport, clock=CLOCK)
    plan = plan_llm(EpStore(store.path, read_only=True), source_id, LlmSettings())
    assert "MODEL_NOT_SELECTED" in plan["blockers"] and plan["external_requests"] == 0
    assert not transport.payloads and not store.llm_history(source_id)


def test_schema_five_stays_readonly_and_cli_cannot_accidentally_execute(observed):
    store, _, source_id = seeded(observed)
    with store.connection() as db:
        db.execute("DROP TABLE ep_llm_calls")
        db.execute("UPDATE ep_schema SET version=5")
    assert EpStore(store.path, read_only=True).schema_version == 5
    env = {k: v for k, v in os.environ.items() if not k.startswith("EP_LLM_")}
    for args in (["llm-plan", source_id, "--model", "gpt-5.4-mini"], ["llm-history", source_id]):
        proc = subprocess.run([sys.executable, str(ROOT / "scripts/run_ep_radar.py"), "--db", str(store.path), *args],
                              capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
    proc = subprocess.run([sys.executable, str(ROOT / "scripts/run_ep_radar.py"), "--db", str(store.path),
        "llm-extract", source_id], capture_output=True, text=True, env=env)
    assert proc.returncode == 2
    assert EpStore(store.path, read_only=True).schema_version == 5
    assert EpStore(store.path).schema_version == 6


def test_http_transport_no_redirect_proxy_retry_or_key_echo(monkeypatch):
    import requests
    captured = {}
    class Reply:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): yield b'{"status":"completed"}'
    class Session:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def post(self, endpoint, **kwargs):
            captured.update(endpoint=endpoint, trust_env=self.trust_env, **kwargs)
            return Reply()
    monkeypatch.setattr(requests, "Session", Session)
    assert OpenAIResponsesTransport("dummy-test-key").generate({})["status"] == "completed"
    assert captured["endpoint"] == "https://api.openai.com/v1/responses"
    assert captured["trust_env"] is False and captured["allow_redirects"] is False
