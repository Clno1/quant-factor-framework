from copy import deepcopy

import pytest

from src.breakouts.ep.llm_contract import prepare_request, validate_response, response_schema
from test_ep_llm import evidence_source, envelope, claim as legacy_claim
from test_ep_llm_batches import claim


def validate(proposal, extra=()):
    request = prepare_request(evidence_source(), batch="eps")
    request["untrusted_paragraphs"].extend(extra)
    return validate_response(request, envelope(request, [proposal]) | {"scope_status": "UNCERTAIN"})


def test_context_schema_and_exact_offsets():
    proposal = claim()
    before = deepcopy(proposal)
    result = validate(proposal)
    assert not result["rejected"] and proposal == before
    accepted = result["accepted"][0]
    paragraphs = {p["id"]: p["text"] for p in prepare_request(evidence_source())["untrusted_paragraphs"]}
    for anchor in accepted["evidence_localization"]["anchors"]:
        for span in anchor["occurrences"]:
            text = paragraphs[anchor["paragraph_id"]]
            assert text[span["quote_start"]:span["quote_end"]] == anchor["quote"]
            assert text[span["text_start"]:span["text_end"]] == anchor["text"]
        assert anchor["association_verified"] is False
    schema = response_schema(prepare_request(evidence_source(), batch="eps"))
    assert schema["$defs"]["Claim"]["properties"]["context_evidence"]["maxItems"] == 6
    assert schema["$defs"]["ContextEvidence"]["properties"]["quote"]["maxLength"] == 400
    assert accepted["financial_semantics_verified"] is False
    assert result["eligible_for_rating"] is False


def test_missing_citation_is_not_repaired_by_global_search():
    proposal = claim()
    proposal["context_evidence"] = [a for a in proposal["context_evidence"] if a["role"] != "SUBJECT"]
    result = validate(proposal)
    assert "SUBJECT_TEXT_CONTEXT_ANCHOR_REQUIRED" in result["rejected"][0]["reasons"]


@pytest.mark.parametrize("change", [
    {"paragraph_id": "not-in-selected-input"}, {"quote": "fabricated"}, {"text": "Other Corp"},
    {"role": "PERIOD"}, {"text": "example"},
])
def test_forged_wrong_role_or_normalized_context_rejected(change):
    proposal = claim()
    proposal["context_evidence"][0].update(change)
    assert validate(proposal)["rejected"]


def test_split_period_remains_fragments_not_a_synthetic_date():
    proposal = claim(period_text=None)
    rows = [{"id": "head1", "text": "Three Months Ended July 31,"}, {"id": "head2", "text": "2026 2025"}]
    proposal["context_evidence"] += [{"role": "PERIOD", "text": p["text"], "paragraph_id": p["id"], "quote": p["text"]} for p in rows]
    accepted = validate(proposal, rows)["accepted"][0]
    assert accepted["period_text"] is None
    assert "PERIOD_FRAGMENTS_NOT_ASSEMBLED" in accepted["review_required"]
    proposal["period_text"] = "Three Months Ended July 31, 2026"
    assert "PERIOD_TEXT_CONTEXT_ANCHOR_REQUIRED" in validate(proposal, rows)["rejected"][0]["reasons"]


def test_scale_exception_and_unrelated_context_never_prove_applicability():
    proposal = claim()
    proposal["unit_scale_text"] = "in millions"
    row = {"id": "other-table", "text": "Other Corp (in millions, except per share data)"}
    proposal["context_evidence"].append({"role": "SCALE", "text": "in millions", "paragraph_id": row["id"], "quote": row["text"]})
    accepted = validate(proposal, [row])["accepted"][0]
    assert accepted["financial_semantics_verified"] is False
    assert "UNIT_SCALE_AND_EXCEPTION_APPLICABILITY_REQUIRES_REVIEW" in accepted["review_required"]
    assert accepted["evidence_localization"]["association_verified"] is False
    proposal["unit_scale_text"] = "in thousands"
    assert validate(proposal, [row])["rejected"]


def test_duplicate_locations_do_not_choose_an_arbitrary_occurrence():
    proposal = claim(subject_text="Example")
    proposal["context_evidence"][0].update(paragraph_id="duplicate", quote="Example Example", text="Example")
    accepted = validate(proposal, [{"id": "duplicate", "text": "Example Example"}])["accepted"][0]
    assert len(accepted["evidence_localization"]["anchors"][0]["occurrences"]) == 2
    assert "CONTEXT_LOCATION_AMBIGUOUS" in accepted["review_required"]


def test_legacy_contract_unchanged_and_new_fields_not_silently_accepted():
    request = prepare_request(evidence_source())
    assert validate_response(request, envelope(request, [legacy_claim()]))["accepted"]
    assert validate_response(request, envelope(request, [claim()]))["rejected"][0]["reasons"] == ["INVALID_CLAIM_SCHEMA"]
    assert "unit_scale_text" not in response_schema(request)["$defs"]["Claim"]["properties"]


def test_overlapping_and_repetitive_context_locations_are_bounded():
    from src.breakouts.ep.llm_evidence import locate_context, MAX_LOCATIONS

    proposal = claim(subject_text=None, period_text=None, unit_text=None)
    proposal["context_evidence"] = [{"role": "SUBJECT", "paragraph_id": "repeat", "quote": "aa", "text": "aa"}]
    result = locate_context(proposal, {"repeat": "aaa"})
    assert [span["text_start"] for span in result["anchors"][0]["occurrences"]] == [0, 1]
    assert result["anchors"][0]["location_status"] == "AMBIGUOUS"
    result = locate_context(proposal, {"repeat": "a" * 60000})
    assert len(result["anchors"][0]["occurrences"]) == MAX_LOCATIONS
    assert result["anchors"][0]["locations_truncated"] is True


def test_unicode_offsets_are_codepoints_not_utf8_bytes():
    from src.breakouts.ep.llm_evidence import locate_context

    proposal = claim(subject_text=None, period_text=None, unit_text=None)
    text = "\u4e2d\u6587 Example"
    proposal["context_evidence"] = [{"role": "SUBJECT", "paragraph_id": "unicode", "quote": text, "text": "Example"}]
    span = locate_context(proposal, {"unicode": text})["anchors"][0]["occurrences"][0]
    assert span["text_start"] == 3 and span["text_end"] == 10
