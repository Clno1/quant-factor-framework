from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket

import pytest

from src.breakouts.ep.llm_observations import (
    audit_previous_choices, build_observation_catalog, observation_prompt, validate_observation_choice,
)
from src.breakouts.ep.llm_span_selection import packet_from_archived_request
from src.breakouts.ep.models import digest

FIXTURE = Path(__file__).parent / "fixtures/ep_live_selection_20260910.json"


def row():
    return next(r for r in json.loads(FIXTURE.read_text())["records"] if r["ticker"] == "PLAB")


def changed(transform):
    prepared = deepcopy(row()["packet"]["validation_request"])
    transform(prepared["untrusted_paragraphs"])
    return packet_from_archived_request(prepared)


def replace_text(paragraphs, old, new):
    for p in paragraphs:
        p["text"] = p["text"].replace(old, new)


def test_real_eps_units_bind_all_six_values_offline_without_mutation(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    sample = row()
    before = deepcopy(sample)
    catalog = build_observation_catalog(sample["packet"])
    assert sample == before and catalog == build_observation_catalog(sample["packet"])
    units = catalog["observations"]
    assert [u["value_text"] for u in units] == ["$0.49", "$0.39", "$0.54", "$0.50", "$0.51", "$0.42"]
    assert units[3]["period_text"] == "third quarter of fiscal year 2026 ended August 2, 2026"
    assert units[4]["period_text"] == "third quarter of 2025"
    assert all(u["subject_text"] == "Photronics, Inc." for u in units)
    assert all(u["share_basis"] == "DILUTED" and not u["financial_semantics_verified"] for u in units)
    assert all(u["unit_scale"] == "PER_SHARE_NOT_INCOME_SCALE" for u in units)
    blocks = {b["paragraph_id"]: b["text"] for b in sample["packet"]["request"]["untrusted_blocks"]}
    for unit in units:
        for evidence in unit["evidence"].values():
            text = blocks[evidence["paragraph_id"]]
            assert text[evidence["start"]:evidence["end"]] == evidence["text"]
            assert digest(text) == evidence["paragraph_hash"]


def test_old_model_wrong_period_is_not_silently_repaired():
    sample = row()
    audit = audit_previous_choices(sample["packet"], sample["response"])
    assert audit["items"][0]["status"] == "BOUND_MATCH_REQUIRES_REVIEW"
    assert audit["items"][1]["reasons"] == ["PERIOD_SELECTION_OUTSIDE_BOUND_OBSERVATION"]
    assert audit["items"][1]["status"] == "BLOCKED_OR_UNBOUND"


def choice(catalog, ids):
    return {"catalog_hash": catalog["catalog_hash"], "scope_status": "COMPLETE_FOR_CATALOG", "observation_ids": ids}


def test_whole_unit_selection_cannot_mix_fields_or_select_prior_as_current():
    packet = row()["packet"]
    catalog = build_observation_catalog(packet)
    current = [u["observation_id"] for u in catalog["observations"] if u["period_role"] == "CURRENT_REPORTED"]
    response = choice(catalog, current)
    result = validate_observation_choice(packet, catalog, response)
    assert [u["value_text"] for u in result["accepted"]] == ["$0.49", "$0.50"]
    assert all("2026 ended" in u["period_text"] for u in result["accepted"])
    assert result["external_requests"] == 0 and not result["eligible_for_rating"]
    response["period"] = "2025"
    with pytest.raises(ValueError, match="INVALID_OBSERVATION_CHOICE"):
        validate_observation_choice(packet, catalog, response)
    historical = catalog["observations"][4]["observation_id"]
    result = validate_observation_choice(packet, catalog, choice(catalog, [historical, "unknown", current[0], current[0]]))
    assert [r["reason"] for r in result["rejected"]] == ["COMPARATIVE_NOT_CURRENT", "UNKNOWN_OBSERVATION_ID", "DUPLICATE_OBSERVATION_ID"]


def test_catalog_cannot_be_tampered_even_with_recomputed_hash():
    packet = row()["packet"]
    catalog = build_observation_catalog(packet)
    catalog["observations"][0]["period_text"] = "2025"
    catalog["catalog_hash"] = digest({k: v for k, v in catalog.items() if k != "catalog_hash"})
    with pytest.raises(ValueError, match="OBSERVATION_CATALOG_CHANGED"):
        validate_observation_choice(packet, catalog, choice(catalog, []))


@pytest.mark.parametrize("old,new", [
    ("NASDAQ:PLAB", "NASDAQ:OTHER"),
    ("today reported financial results", "expects financial results"),
    ("Photronics, Inc. (NASDAQ", "Other Company (NASDAQ"),
])
def test_current_period_requires_matching_issuer_report_context(old, new):
    packet = changed(lambda ps: replace_text(ps, old, new))
    catalog = build_observation_catalog(packet)
    assert not catalog["observations"]
    assert any(u["reason"] == "CURRENT_PERIOD_OR_ISSUER_LINK_UNRESOLVED" for u in catalog["unbound"])


def test_ambiguous_current_period_is_not_latest_date_guess():
    def transform(ps):
        ps.append({"id": "p9999", "text": ps[0]["text"].replace("2026", "2025")})
    assert not build_observation_catalog(changed(transform))["observations"]


@pytest.mark.parametrize("old,new", [
    ("share, compared with", "share; guidance next quarter compared with"),
    ("third quarter of 2025", "unidentified quarter"),
    ("$0.49 per", "49% per"),
])
def test_unsupported_or_malformed_prose_never_yields_partial_units(old, new):
    packet = changed(lambda ps: replace_text(ps, old, new))
    catalog = build_observation_catalog(packet)
    assert not any(u["basis"] == "GAAP" for u in catalog["observations"])


def test_other_batches_remain_explicitly_unbound_not_filled_from_model():
    for sample in json.loads(FIXTURE.read_text())["records"]:
        if sample["ticker"] == "PLAB":
            continue
        catalog = build_observation_catalog(sample["packet"])
        assert not catalog["observations"] and catalog["unbound"]
        assert all(u["reason"] == "BATCH_NOT_YET_SUPPORTED" for u in catalog["unbound"])


def test_new_prompt_only_offers_current_whole_units_and_empty_is_not_complete():
    packet = row()["packet"]
    catalog = build_observation_catalog(packet)
    preview = observation_prompt(catalog)
    assert len(preview["user"]["observations"]) == 2
    assert not preview["live_provider_connected"]
    assert set(preview["response_schema"]["properties"]) == {"catalog_hash", "scope_status", "observation_ids"}
    result = validate_observation_choice(packet, catalog, choice(catalog, []))
    assert not result["exhaustiveness_verified"]


def test_source_revision_changes_units_and_rejects_old_choices():
    packet = row()["packet"]
    old = build_observation_catalog(packet)
    prepared = deepcopy(packet["validation_request"])
    prepared["text_revision"] = "new-revision"
    new_packet = packet_from_archived_request(prepared)
    new = build_observation_catalog(new_packet)
    assert {u["observation_id"] for u in old["observations"]}.isdisjoint(
        u["observation_id"] for u in new["observations"])
    with pytest.raises(ValueError, match="OBSERVATION_CATALOG_VERSION_MISMATCH"):
        validate_observation_choice(new_packet, new, choice(old, []))


def test_parser_not_bound_to_plab_ticker_or_values():
    prepared = deepcopy(row()["packet"]["validation_request"])
    replace_text(prepared["untrusted_paragraphs"], "Photronics, Inc.", "Example Holdings")
    replace_text(prepared["untrusted_paragraphs"], "NASDAQ:PLAB", "NYSE:EXAMPLE")
    replace_text(prepared["untrusted_paragraphs"], "$0.50", "$1.27")
    prepared["ticker"] = "EXAMPLE"
    catalog = build_observation_catalog(packet_from_archived_request(prepared))
    assert len(catalog["observations"]) == 6
    assert catalog["observations"][3]["value_text"] == "$1.27"
    assert all(u["subject_text"] == "Example Holdings" for u in catalog["observations"])


def test_inline_current_period_conflict_does_not_override_intro():
    packet = changed(lambda ps: replace_text(ps, "$0.50 per diluted share, compared",
                                            "$0.50 per diluted share in the third quarter of 2025, compared"))
    catalog = build_observation_catalog(packet)
    assert not any(u["basis"] == "NON_GAAP" for u in catalog["observations"])
    assert any(p["reason"] == "CURRENT_PERIOD_CONFLICT" for p in catalog["unbound"])


def test_prompt_export_matches_actual_request_and_report_is_not_model_accuracy(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))
    path = Path(__file__).resolve().parents[1] / "reviews/2026-09-10-ep-span-selection/run_observations.py"
    spec = importlib.util.spec_from_file_location("observation_review", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    fixture = json.loads(FIXTURE.read_text())
    before = deepcopy(fixture)
    payload = runner.previous_payload(fixture)
    assert digest({"provider": "kimi-cn", "payload": payload}) == row()["request_key"]
    assert fixture == before
    report = runner.run(fixture)
    assert report["external_requests"] == 0 and report["live_model_selection_accuracy"] is None
    plab = next(r for r in report["reports"] if r["ticker"] == "PLAB")
    assert len(plab["reference_choice_validation"]["accepted"]) == 2
    assert plab["reference_choice_type"] == "PROGRAM_SELECTED_CURRENT_UNITS_NOT_MODEL_OUTPUT"
