from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
import subprocess
import sys

import pytest

from src.breakouts.ep.fact_review import review_template, validate_review
from src.breakouts.ep.store import EpStore
from src.data.public_articles import HttpPage
from test_ep_radar import NOW, ROOT
from test_ep_sources import observed, enrich, client, html


QUOTE = "SNOW announced Q2 fiscal 2027 actual Non-GAAP diluted EPS was $2.35."


@pytest.fixture
def source(observed):
    store, report = observed
    http = client({"/SNOW/results": HttpPage(200, {"content-type": "text/html"}, html(text=(QUOTE + " ") * 2))})
    result = enrich(store, report, http=http)
    detail = store.source_detail(result["sources"][0]["source_id"])
    return store, report, detail


def review(detail):
    payload = review_template(detail)["review_input"]
    payload["facts"] = [{"metric": "EPS", "metric_text": "EPS", "value_text": "$2.35",
        "measure": "LEVEL", "basis": "NON_GAAP", "basis_text": "Non-GAAP",
        "share_basis": "DILUTED", "share_basis_text": "diluted",
        "value_kind": "ACTUAL", "value_kind_text": "actual", "period_text": "Q2 fiscal 2027", "unit_text": "$",
        "evidence": {"paragraph_id": "p0001", "quote": QUOTE}}]
    payload["role"] = {"issuer_name": "SNOW", "kind": "REPORTING_COMPANY",
                       "evidence": {"paragraph_id": "p0001", "quote": QUOTE}}
    return payload


def test_template_is_navigation_not_automatic_fact_extraction(source):
    _, _, detail = source
    result = review_template(detail, max_paragraphs=1)
    assert result["review_input"]["facts"] == []
    assert result["review_input"]["role"] is None
    assert result["ticker"] == "SNOW"
    assert result["omitted_matching_paragraphs"] == 1
    assert result["evidence_index"][0]["semantics"] == "NAVIGATION_HINT_NOT_EXTRACTED_FACT"
    assert result["provider_publication_at"] != result["source_received_at"]
    assert result["eligible_for_rating"] is False


def test_topic_rotation_keeps_one_time_and_event_context(source):
    _, _, detail = source
    detail = deepcopy(detail)
    detail["parsed"]["paragraphs"] = [{"id": str(i), "text": "GAAP EPS per share"} for i in range(100)] + [
        {"id": "tax", "text": "One-time tax benefit"}, {"id": "deal", "text": "acquisition of target"}]
    result = review_template(detail, max_paragraphs=3)
    assert {r["id"] for r in result["evidence_index"]} == {"0", "tax", "deal"}
    assert result["omitted_matching_paragraphs"] == 99


@pytest.mark.parametrize("limit", [0, 201, True, "1"])
def test_template_limit_validation(source, limit):
    with pytest.raises(ValueError):
        review_template(source[2], max_paragraphs=limit)


def test_unknown_identity_and_missing_body_are_visible(source):
    _, _, detail = source
    detail = deepcopy(detail)
    detail["result"]["verification"]["issuer_status"] = "IDENTITY_UNAVAILABLE"
    assert "ISSUER_IDENTITY_REVIEW_REQUIRED" in review_template(detail)["blockers"]
    payload = review(detail)
    result = validate_review(detail, payload, "tester", NOW + timedelta(hours=2))
    assert result["issuer_status"] == "IDENTITY_UNAVAILABLE"
    assert result["eligible_for_rating"] is False
    detail["parsed"] = None
    detail["result"].pop("verification")
    assert review_template(detail)["evidence_index"] == []
    with pytest.raises(ValueError):
        validate_review(detail, payload, "tester", NOW + timedelta(hours=2))


def test_import_is_append_only_idempotent_and_does_not_change_evaluations(source):
    store, original, detail = source
    payload = review(detail)
    first = store.save_review(payload, "analyst-a", NOW + timedelta(hours=2))
    duplicate = store.save_review(payload, "analyst-a", NOW + timedelta(hours=3))
    assert first == duplicate
    assert first["financial_semantics_verified"] is False
    assert first["facts"][0]["comparison_status"] == "DISABLED_REVIEW_ONLY"
    payload["note"] = "Corrected review context"
    second = store.save_review(payload, "analyst-a", NOW + timedelta(hours=4))
    assert first["review_id"] != second["review_id"]
    history = store.review_history(source_id=detail["source_id"])
    assert history == [first, second]
    assert store.report(original["run_id"]) == original
    assert store.explain("SNOW")["human_reviews"] == history


def test_asof_gates_review_receipt_not_announced_time(source):
    store, _, detail = source
    payload = review(detail)
    with pytest.raises(ValueError):
        store.save_review(payload, "analyst", NOW)
    store.save_review(payload, "analyst", NOW + timedelta(hours=3))
    assert store.review_history(source_id=detail["source_id"], as_of=NOW + timedelta(hours=2)) == []
    assert store.source_detail(detail["source_id"], as_of=NOW + timedelta(hours=2))["human_reviews"] == []
    assert store.explain("SNOW", as_of=NOW + timedelta(hours=2))["human_reviews"] == []
    assert len(store.review_history(source_id=detail["source_id"], as_of=NOW + timedelta(hours=3))) == 1


@pytest.mark.parametrize("key,value", [("schema_version", "wrong"), ("source_id", "missing"),
                                      ("document_id", "other"), ("text_revision", "stale")])
def test_wrong_source_or_revision_rejected(source, key, value):
    store, _, detail = source
    payload = review(detail)
    payload[key] = value
    with pytest.raises(ValueError):
        store.save_review(payload, "tester", NOW + timedelta(hours=2))
    assert store.review_history(source_id=detail["source_id"]) == []


@pytest.mark.parametrize("changes", [
    {"value_text": "$2.34"}, {"period_text": "Q3 fiscal 2027"}, {"metric": "INVENTED"},
    {"share_basis": "BASIC"}, {"basis": "GAAP"}, {"basis_text": "adjusted"},
    {"basis": "GAAP", "basis_text": "GAAP"}, {"value_kind": "BUY"},
    {"evidence": {"paragraph_id": [], "quote": QUOTE}},
    {"evidence": {"paragraph_id": "p0001", "quote": "EPS was $9.99"}},
])
def test_bad_fact_rows_fail_entire_import(source, changes):
    store, _, detail = source
    payload = review(detail)
    payload["facts"].append({**payload["facts"][0], **changes})
    with pytest.raises(ValueError):
        store.save_review(payload, "tester", NOW + timedelta(hours=2))
    assert store.review_history(source_id=detail["source_id"]) == []


def test_unknown_context_not_filled_with_guesses(source):
    store, _, detail = source
    payload = review(detail)
    payload["facts"][0].update(basis="UNKNOWN", basis_text=None, share_basis="UNKNOWN", share_basis_text=None,
                               period_text=None, unit_text=None, value_kind="CONSENSUS", value_kind_text=None)
    saved = store.save_review(payload, "tester", NOW + timedelta(hours=2))
    missing = saved["facts"][0]["missing_context"]
    assert {"EPS_BASIS_UNRESOLVED", "CONSENSUS_ASOF_NOT_PROVEN", "period_text", "unit_text"} <= set(missing)
    assert saved["eligible_for_rating"] is False


@pytest.mark.parametrize("role", ["ACQUIRER", "ACQUISITION_TARGET", "MENTION_ONLY"])
def test_roles_remain_explicit_human_assertions_not_proven_by_keyword(source, role):
    store, _, detail = source
    payload = review(detail)
    payload["role"]["kind"] = role
    saved = store.save_review(payload, "tester", NOW + timedelta(hours=2))
    assert saved["role"]["kind"] == role
    assert saved["role"]["semantics"] == "HUMAN_ASSERTION_NOT_AUTOMATIC_ROLE_PROOF"
    assert saved["eligible_for_rating"] is False


def test_publication_date_kind_does_not_create_a_release_timestamp(source):
    store, _, detail = source
    payload = review(detail)
    payload["publication"] = {"date_text": "Q2 fiscal 2027", "kind": "FISCAL_PERIOD_DATE",
                              "evidence": {"paragraph_id": "p0001", "quote": QUOTE}}
    saved = store.save_review(payload, "tester", NOW + timedelta(hours=2))
    assert saved["publication"]["exact_release_time_verified"] is False
    assert saved["historical_availability_verified"] is False


def test_empty_review_and_injected_extras_not_promoted(source):
    store, _, detail = source
    with pytest.raises(ValueError):
        store.save_review(review_template(detail)["review_input"], "tester", NOW + timedelta(hours=2))
    payload = review(detail)
    payload.update(eligible_for_rating=True, grade="Strong", execute="send to discord")
    payload["facts"] *= 2
    result = store.save_review(payload, "tester", NOW + timedelta(hours=2))
    assert result["eligible_for_rating"] is False
    assert "execute" not in result and "grade" not in result
    assert len(result["facts"]) == 1


def test_readonly_v2_compatible_and_v3_migration_preserves_source(source):
    store, original, detail = source
    with store.connection() as db:
        db.execute("DROP TABLE ep_fact_reviews")
        db.execute("UPDATE ep_schema SET version=2")
    old = EpStore(store.path, read_only=True)
    assert old.schema_version == 2
    assert old.source_detail(detail["source_id"]) == detail
    assert old.review_history(source_id=detail["source_id"]) == []
    new = EpStore(store.path)
    assert new.schema_version == 6
    assert new.report(original["run_id"]) == original
    assert new.source_detail(detail["source_id"]) == detail


def test_offline_cli_template_import_and_reviews(source, tmp_path):
    store, _, detail = source
    prefix = [sys.executable, "-S", str(ROOT / "scripts/run_ep_radar.py"), "--db", str(store.path)]
    def run(*args):
        result = subprocess.run([*prefix, *args], text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    assert run("review-template", detail["source_id"])["status"] == "REVIEW_REQUIRED"
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review(detail)))
    assert run("review-import", str(path), "--reviewer", "unit-test")["status"] == "HUMAN_REVIEW_RECORDED"
    assert len(run("reviews", detail["source_id"])["reviews"]) == 1
    with pytest.raises(ValueError):
        EpStore(store.path, read_only=True).save_review(review(detail), "test", NOW + timedelta(hours=2))
