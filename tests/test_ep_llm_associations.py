from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from src.breakouts.ep.llm_associations import audit_associations
from src.breakouts.ep.llm_span_selection import prepare_span_packet, validate_selection
from test_ep_llm_span_selection import references, synthetic
from test_ep_analysis import source
from test_ep_discovery import EXHIBIT

FIXTURE = Path(__file__).parent / "fixtures/ep_live_selection_20260910.json"


def test_actual_responses_are_immutable_and_wrong_period_is_blocked_offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network forbidden"))
    fixture = json.loads(FIXTURE.read_text())
    before = deepcopy(fixture)
    reports = {r["ticker"]: audit_associations(r["packet"], r["response"]) for r in fixture["records"]}
    assert fixture == before
    assert fixture["origin"] == "ACTUAL_KIMI_RESPONSES_20260910"
    assert sum(i["status"] == "BLOCKED" for r in reports.values() for i in r["items"]) == 5
    assert all(r["external_requests"] == 0 and not r["eligible_for_rating"] for r in reports.values())
    assert len(reports["PLAB"]["text_validation"]["accepted"]) == 2
    good, bad = reports["PLAB"]["items"]
    assert good["status"] == "REVIEW_REQUIRED" and not good["checks"]["contradictions"]
    assert bad["text_validation_status"] == "ACCEPTED" and bad["status"] == "BLOCKED"
    assert "PERIOD_CROSSES_COMPARISON_BRANCH" in bad["checks"]["contradictions"]
    assert all("ACCOUNTING_BASIS_EVIDENCE_MISMATCH" in i["checks"]["contradictions"]
               for i in reports["GTLB"]["items"])
    amount, per_share = reports["ANF"]["items"]
    assert "SCALED_TOTAL_ASSIGNED_PER_SHARE_BASIS" in amount["checks"]["contradictions"]
    assert "SCALED_TOTAL_ASSIGNED_PER_SHARE_BASIS" not in per_share["checks"]["contradictions"]
    assert "SUBJECT_EVIDENCE_MISSING" in amount["checks"]["unresolved"]
    assert all("UNIT_SPAN_IS_NOT_UNIT_ONLY" in i["checks"]["contradictions"] for i in reports["ANF"]["items"])


def example(text, value="$0.24", period="Q2 FY 2027", **overrides):
    item = source(text, "Supporting text.")
    item["result"]["final_url"] = EXHIBIT
    packet = prepare_span_packet(item, batch="eps")
    ref = references.ref
    selection = {**references.common("EPS"), "metric_span": ref("p0002", "EPS"),
                 "value_span": ref("p0002", value), "subject_span": ref("p0002", "Example"),
                 "period_spans": [ref("p0002", period)], "unit_span": ref("p0002", "$"),
                 **overrides}
    return packet, references.selected_response(packet, selection)


@pytest.mark.parametrize("connector", ["compared with", "compared to", "versus", "vs.", "whereas"])
def test_comparison_period_not_borrowed_from_other_value(connector):
    packet, response = example(f"Example reported EPS $0.24 in Q2 FY 2027, {connector} $0.20 in Q2 FY 2026.", period="Q2 FY 2026")
    audit = audit_associations(packet, response)
    assert "PERIOD_CROSSES_COMPARISON_BRANCH" in audit["items"][0]["checks"]["contradictions"]
    # An intentionally selected historical comparison is not intrinsically an error.
    packet, response = example(f"Example reported EPS $0.24 in Q2 FY 2027, {connector} $0.20 in Q2 FY 2026.", value="$0.20", period="Q2 FY 2026")
    checks = audit_associations(packet, response)["items"][0]["checks"]
    assert "PERIOD_CROSSES_COMPARISON_BRANCH" not in checks["contradictions"]


def test_same_branch_is_not_financial_certification():
    packet, response = synthetic()
    audit = audit_associations(packet, response)
    assert audit["items"][0]["status"] == "REVIEW_REQUIRED"
    assert audit["items"][0]["checks"]["contradictions"] == []
    assert not audit["financial_semantics_verified"]
    assert "SUBJECT_IDENTITY_AND_SCOPE_NOT_CERTIFIED" in audit["items"][0]["checks"]["unresolved"]


def test_basis_subject_and_share_basis_cannot_cross_comparison_branch():
    text = "Example reported EPS $0.24 in Q2 FY 2027; compared with Peer non-GAAP diluted EPS $0.20 in Q2 FY 2026."
    ref = references.ref
    packet, response = example(text, subject_span=ref("p0002", "Peer"), basis="NON_GAAP",
                               basis_span=ref("p0002", "non-GAAP"), share_basis="DILUTED",
                               share_basis_span=ref("p0002", "diluted"))
    checks = audit_associations(packet, response)["items"][0]["checks"]
    assert {"SUBJECT_CROSS_BRANCH_ASSOCIATION_UNRESOLVED", "BASIS_CROSS_BRANCH_ASSOCIATION_UNRESOLVED",
            "SHARE_BASIS_CROSS_BRANCH_ASSOCIATION_UNRESOLVED"} <= set(checks["unresolved"])


def test_unknown_missing_and_cross_paragraph_evidence_not_guessed():
    packet, response = synthetic()
    selection = response["selections"][0]
    selection["subject_span"] = None
    selection["period_spans"] = []
    selection["basis"] = "UNKNOWN"
    selection["basis_span"] = None
    checks = audit_associations(packet, response)["items"][0]["checks"]
    assert {"SUBJECT_EVIDENCE_MISSING", "PERIOD_EVIDENCE_MISSING", "ACCOUNTING_BASIS_NOT_CERTIFIED"} <= set(checks["unresolved"])
    fixture = json.loads(FIXTURE.read_text())
    plab = next(r for r in fixture["records"] if r["ticker"] == "PLAB")
    checks = audit_associations(plab["packet"], plab["response"])["items"][0]["checks"]
    assert "PERIOD_CROSS_PARAGRAPH_ASSOCIATION_UNRESOLVED" in checks["unresolved"]


def test_gaap_substring_in_non_gaap_is_not_gaap_evidence():
    ref = references.ref
    packet, response = example("Example reported non-GAAP EPS $0.24 in Q2 FY 2027.",
                               basis="GAAP", basis_span=ref("p0002", "GAAP"))
    assert "GAAP_SELECTED_INSIDE_NON_GAAP" in audit_associations(packet, response)["items"][0]["checks"]["contradictions"]


def test_invalid_ids_never_reach_relationship_checks():
    packet, response = synthetic()
    response["selections"][0]["value_span"]["start_id"] = "s" + "0" * 24
    report = audit_associations(packet, response)
    assert report["items"][0]["status"] == "BLOCKED"
    assert report["items"][0]["text_rejection_reasons"] == ["UNKNOWN_FRAGMENT_ID"]
    assert report["text_validation"] == validate_selection(packet, response)


def test_next_amount_in_comparison_list_has_its_own_period():
    text = "Example reported EPS $0.24 in Q2 FY 2027; compared with $0.20 in Q2 FY 2026 and $0.18 in Q1 FY 2027."
    packet, response = example(text, value="$0.20", period="Q1 FY 2027")
    assert "PERIOD_CROSSES_COMPARISON_BRANCH" in audit_associations(packet, response)["items"][0]["checks"]["contradictions"]
    packet, response = example(text, value="$0.18", period="Q1 FY 2027")
    assert "PERIOD_CROSSES_COMPARISON_BRANCH" not in audit_associations(packet, response)["items"][0]["checks"]["contradictions"]


def test_explicitly_scaled_per_share_amount_is_not_automatically_total():
    ref = references.ref
    packet, response = example("Example reported diluted EPS $1 million per diluted share in Q2 FY 2027.",
                               value="$1 million per diluted share", share_basis="DILUTED",
                               share_basis_span=ref("p0002", "diluted"))
    checks = audit_associations(packet, response)["items"][0]["checks"]
    assert "SCALED_TOTAL_ASSIGNED_PER_SHARE_BASIS" not in checks["contradictions"]
    assert "SCALED_PER_SHARE_AMOUNT_REQUIRES_REVIEW" in checks["unresolved"]


def test_packet_hash_is_checked_before_auditing():
    packet, response = synthetic()
    packet["request"]["untrusted_blocks"][0]["text"] += " changed"
    with pytest.raises(ValueError, match="LOCAL_PACKET_CHANGED"):
        audit_associations(packet, response)


def test_and_amount_without_comparison_is_not_assumed_to_be_prior_period():
    packet, response = example("Example reported EPS $0.24 and $0.20 in Q2 FY 2027.")
    checks = audit_associations(packet, response)["items"][0]["checks"]
    assert "PERIOD_CROSSES_COMPARISON_BRANCH" not in checks["contradictions"]
    assert "PERIOD_VALUE_SEMANTICS_NOT_CERTIFIED" in checks["unresolved"]
