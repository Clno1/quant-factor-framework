from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

from src.breakouts.ep.llm_span_selection import (
    prepare_span_packet, packet_from_archived_request, validate_selection, selection_schema,
    MAX_FRAGMENTS, MAX_WIRE_BYTES,
)
from test_ep_llm import evidence_source

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "reviews/2026-09-10-ep-span-selection"
spec = importlib.util.spec_from_file_location("span_reference_cases", REVIEW / "reference_cases.py")
references = importlib.util.module_from_spec(spec)
spec.loader.exec_module(references)


def synthetic():
    packet = prepare_span_packet(evidence_source(), batch="eps")
    ref = references.ref
    selection = {**references.common("EPS"), "metric_span": ref("p0002", "EPS"),
        "value_span": ref("p0002", "$0.24"), "subject_span": ref("p0002", "Example"),
        "period_spans": [ref("p0002", "Q2 FY 2027")], "unit_span": ref("p0002", "$"),
        "basis_span": ref("p0002", "non-GAAP"), "basis": "NON_GAAP", "share_basis": "DILUTED",
        "share_basis_span": ref("p0002", "diluted"), "value_kind_span": ref("p0002", "reported")}
    return packet, references.selected_response(packet, selection)


def test_only_ids_backfill_exact_source_and_never_mutate_input():
    packet, response = synthetic()
    before = deepcopy((packet, response))
    checked = validate_selection(packet, response)
    assert not checked["rejected"] and len(checked["accepted"]) == 1
    proposal = checked["accepted"][0]
    assert proposal["value_text"] == "$0.24" and proposal["basis_text"] == "non-GAAP"
    assert proposal["copied_by"] == "LOCAL_SOURCE_SLICE"
    assert proposal["financial_semantics_verified"] is False and checked["external_requests"] == 0
    assert (packet, response) == before
    fields = selection_schema(packet["request"]["batch"])["$defs"]["Selection"]["properties"]
    assert not any(k.endswith("_text") or k == "evidence" for k in fields)
    assert fields["metric"]["enum"] == ["EPS"]


def test_stable_ids_bind_document_revision_and_text():
    original = evidence_source()
    packet = prepare_span_packet(original, batch="eps")
    assert packet == prepare_span_packet(original, batch="eps")
    def ids(p):
        return {f["id"] for b in p["request"]["untrusted_blocks"] for f in b["fragments"]}
    changed = deepcopy(original)
    changed["parsed"]["text_revision"] = "different-revision"
    assert not ids(packet) & ids(prepare_span_packet(changed, batch="eps"))
    changed = deepcopy(original)
    changed["document_id"] = "different-document"
    assert not ids(packet) & ids(prepare_span_packet(changed, batch="eps"))


@pytest.mark.parametrize("field", ["value_text", "quote", "start", "instructions"])
def test_model_cannot_supply_text_quotes_or_offsets(field):
    packet, response = synthetic()
    response["selections"][0][field] = "invented"
    with pytest.raises(ValueError, match="INVALID_SELECTION_RESPONSE"):
        validate_selection(packet, response)


def test_unknown_stale_and_cross_paragraph_ids_rejected():
    packet, response = synthetic()
    response["selections"][0]["value_span"]["start_id"] = "s" + "0" * 24
    assert validate_selection(packet, response)["rejected"][0]["reasons"] == ["UNKNOWN_FRAGMENT_ID"]
    packet, response = synthetic()
    response["selections"][0]["value_span"]["end_id"] = next(
        b["fragments"][0]["id"] for b in packet["request"]["untrusted_blocks"] if b["paragraph_id"] != "p0002")
    assert validate_selection(packet, response)["rejected"][0]["reasons"] == ["CROSS_PARAGRAPH_SPAN"]
    response["text_revision"] = "other"
    with pytest.raises(ValueError, match="DOCUMENT_VERSION"):
        validate_selection(packet, response)


def test_catalog_tampering_and_out_of_scope_metric_rejected():
    packet, response = synthetic()
    packet["request"]["untrusted_blocks"][0]["text"] += " changed"
    with pytest.raises(ValueError, match="LOCAL_PACKET_CHANGED"):
        validate_selection(packet, response)
    packet, response = synthetic()
    response["selections"][0]["metric"] = "REVENUE"
    assert "CLAIM_OUTSIDE_BATCH_SCOPE" in validate_selection(packet, response)["rejected"][0]["reasons"]


def test_value_cannot_be_currency_fragment_or_multiple_actuals():
    packet, response = synthetic()
    response["selections"][0]["value_span"] = response["selections"][0]["unit_span"]
    assert validate_selection(packet, response)["rejected"][0]["reasons"] == ["PARTIAL_NUMBER_ATOM"]
    packet, response = synthetic()
    response["selections"][0]["value_span"]["end_id"] = response["selections"][0]["period_spans"][0]["end_id"]
    assert validate_selection(packet, response)["rejected"][0]["reasons"] == ["MULTIPLE_ACTUAL_VALUES"]


def test_partial_input_and_budget_caps_are_visible():
    source = evidence_source()
    source["parsed"]["paragraphs"].append({"id": "long", "text": "token " * (MAX_FRAGMENTS + 10)})
    packet = prepare_span_packet(source, batch="eps")
    coverage = packet["request"]["coverage"]
    assert not coverage["complete"] and "long" in coverage["omitted_for_budget"]
    assert coverage["fragment_count"] <= MAX_FRAGMENTS
    assert len(json.dumps(packet["request"], ensure_ascii=False).encode()) <= MAX_WIRE_BYTES
    with pytest.raises(ValueError, match="UNKNOWN_OR_EMPTY"):
        prepare_span_packet(source, batch="eps", paragraph_ids=["missing"])


def test_wrong_source_and_duplicate_selections():
    source = evidence_source()
    source["result"]["issuer_linkage"] = "UNVERIFIED"
    with pytest.raises(ValueError, match="ISSUER_LINKAGE"):
        prepare_span_packet(source, batch="eps")
    packet, response = synthetic()
    response["selections"] *= 2
    assert len(validate_selection(packet, response)["accepted"]) == 1


def test_twelve_human_references_are_not_model_accuracy(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **kw: pytest.fail("offline test attempted network"))
    monkeypatch.syspath_prepend(str(REVIEW))
    spec = importlib.util.spec_from_file_location("span_offline_review", REVIEW / "run_offline.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    archive = json.loads((ROOT / "tests/fixtures/ep_span_selection_20260910.json").read_text())
    report = runner.run(archive)
    assert report["accepted_reference_cases"] == 12
    assert report["unique_materialized_proposals"] == 10
    assert report["model_selection_accuracy"] is None and report["external_requests"] == 0
    assert all(not r["validation"]["coverage"]["complete"] for r in report["results"])
    proposals = [r["validation"]["accepted"][0] for r in report["results"]]
    assert proposals[6]["value_text"] == "$ (38,574)"
    assert proposals[2]["period_text"] is None and len(proposals[2]["selected_spans"]["period_spans"]) == 2
    assert "PERIOD_FRAGMENTS_NOT_ASSEMBLED" in proposals[2]["review_required"]
    assert all(not p["financial_semantics_verified"] for p in proposals)


def test_number_and_guidance_range_preserve_original_punctuation():
    from test_ep_analysis import source
    from test_ep_discovery import EXHIBIT

    item = source("Example expects revenue $281 - $283 million for Q3 FY 2027.", "Supporting text.")
    item["result"]["final_url"] = EXHIBIT
    packet = prepare_span_packet(item, batch="guidance")
    ref = references.ref
    selection = {**references.common("REVENUE", "COMPANY_GUIDANCE"),
                 "metric_span": ref("p0002", "revenue"), "value_span": ref("p0002", "$281 - $283"),
                 "subject_span": ref("p0002", "Example"), "period_spans": [ref("p0002", "Q3 FY 2027")],
                 "unit_span": ref("p0002", "$"), "unit_scale_span": ref("p0002", "million")}
    response = references.selected_response(packet, selection)
    accepted = validate_selection(packet, response)["accepted"][0]
    assert accepted["value_text"] == "$281 - $283"
    assert accepted["unit_scale_text"] == "million"


def test_empty_result_does_not_prove_complete_discovery():
    packet, response = synthetic()
    response["selections"] = []
    response["scope_status"] = "COMPLETE_FOR_SCOPE"
    result = validate_selection(packet, response)
    assert result["exhaustiveness_verified"] is False
    assert result["model_scope_status"] == "COMPLETE_FOR_SCOPE"


def test_long_evidence_never_silently_trims_context():
    source = evidence_source()
    source["parsed"]["paragraphs"][1]["text"] += " Context must remain visible." * 20
    packet = prepare_span_packet(source, batch="eps")
    _, response = synthetic()
    # Changing the paragraph changes IDs. Explicitly select from the new packet.
    ref = references.ref
    selection = {**references.common("EPS"), "metric_span": ref("p0002", "EPS"),
        "value_span": ref("p0002", "$0.24"), "subject_span": ref("p0002", "Example"),
        "period_spans": [], "unit_span": None}
    response = references.selected_response(packet, selection)
    assert validate_selection(packet, response)["rejected"][0]["reasons"] == ["FULL_PARAGRAPH_EVIDENCE_TOO_LONG"]


def test_missing_source_catalog_and_reversed_range_are_not_guessed():
    packet, response = synthetic()
    span = response["selections"][0]["period_spans"][0]
    span["start_id"], span["end_id"] = span["end_id"], span["start_id"]
    assert validate_selection(packet, response)["rejected"][0]["reasons"] == ["REVERSED_FRAGMENT_RANGE"]
    source = evidence_source()
    with pytest.raises(ValueError, match="NO_PARAGRAPH_FITS"):
        source["parsed"]["paragraphs"] = [{"id": "long", "text": "token " * (MAX_FRAGMENTS + 10)}]
        prepare_span_packet(source, batch="eps")
