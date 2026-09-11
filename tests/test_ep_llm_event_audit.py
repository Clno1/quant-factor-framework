from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

from src.breakouts.ep.llm_event import validate_event
from src.breakouts.ep.llm_event_audit import audit_event, quantity_expressions

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cases():
    rows = json.loads((FIXTURES / "ep_event_live_20260910.json").read_text())["records"]
    bindings = json.loads((FIXTURES / "ep_event_review_bindings_20260910.json").read_text())
    return {row["ticker"]: (row["packet"], row["response"], bindings[row["ticker"]]) for row in rows}


@pytest.mark.parametrize("text", [
    "拥有超过二十万处停车位", "交易预计二零二六年第四季度完成", "EPS为零点五零美元",
    "营收增长百分之二十", "金额壹佰萬元", "两大业务板块", "共三段", "约数百万股",
    "持股一半", "收入翻倍", "增长２０％", "$0.50 per share", "twenty percent growth",
    "more than two hundred thousand spaces", "first quarter closing", "five million dollars",
])
def test_quantity_expressions_preserve_original_offsets(text):
    found = quantity_expressions(text)
    assert found
    assert all(text[m["start"]:m["end"]] == m["text"] for m in found)


@pytest.mark.parametrize("text", [
    "一次性收益", "一致预期", "一体化平台", "进一步整合", "双方业务互补", "千载难逢",
    "同比持平，环比上升", "公司表示可能存在交叉销售机会", "second source validation",
])
def test_common_non_numeric_words_not_misclassified(text):
    assert not quantity_expressions(text)


def test_real_replay_blocks_missing_citations_and_chinese_quantities(cases, monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("offline audit used network"))
    before = deepcopy(cases)
    plab = audit_event(*cases["PLAB"])
    nyax = audit_event(*cases["NYAX"])
    missing = [c for c in plab["items"][1]["reviewed_claims"] if c["missing_paragraph_ids"]]
    assert len(missing) == 2
    assert all(c["missing_paragraph_ids"] == ["p0014"] for c in missing)
    assert "CLAIM_EVIDENCE_NOT_CITED" in plab["items"][1]["reasons"]
    assert [item["status"] for item in plab["items"]] == ["BLOCKED"] * 3
    assert [item["status"] for item in nyax["items"]] == ["BLOCKED", "REVIEW_REQUIRED", "BLOCKED"]
    assert nyax["items"][1]["claim_inventory_complete"]
    assert not nyax["items"][2]["claim_inventory_complete"]
    assert "提供段落未包含" in nyax["items"][2]["unreviewed_spans"][0]["text"]
    assert not nyax["semantic_support_verified"] and not nyax["eligible_for_rating"]
    assert nyax["external_requests"] == 0 and nyax["delivery"] == "DISABLED_SHADOW_ONLY"
    assert cases == before
    for packet, response, _ in cases.values():
        assert audit_event(packet, response)["previous_validation"] == validate_event(packet, response)


def test_no_annotations_never_implies_complete_evidence(cases):
    packet, response, _ = cases["NYAX"]
    item = audit_event(packet, response)["items"][1]
    assert item["status"] == "REVIEW_REQUIRED"
    assert item["unreviewed_spans"] and not item["claim_inventory_complete"]


def test_checked_in_audit_reproduces_from_raw_responses(cases):
    report = json.loads((Path(__file__).parents[1] / "reviews" / "2026-09-10-ep-span-selection" / "event_audit.json").read_text())
    for row in report["records"]:
        assert row["audit"] == audit_event(*cases[row["ticker"]])


@pytest.mark.parametrize("mutation,error", [
    ("packet", "EVENT_REVIEW_PACKET_MISMATCH"),
    ("note", "EVENT_REVIEW_NOTE_MISMATCH"),
    ("duplicate", "EVENT_REVIEW_NOTE_MISMATCH"),
    ("unknown", "EVENT_REVIEW_UNKNOWN_PARAGRAPH"),
    ("quote", "EVENT_REVIEW_SPAN_NOT_UNIQUE"),
    ("claim", "EVENT_REVIEW_SPAN_NOT_UNIQUE"),
    ("overlap", "EVENT_REVIEW_CLAIMS_OVERLAP"),
])
def test_review_cannot_silently_follow_changed_evidence(cases, mutation, error):
    packet, response, review = cases["NYAX"]
    claim = review["notes"][0]["claims"][0]
    if mutation == "packet":
        review["packet_hash"] = "different"
    elif mutation == "note":
        response["notes"][0]["text"] += "已经交割。"
    elif mutation == "duplicate":
        review["notes"].append(deepcopy(review["notes"][0]))
    elif mutation == "unknown":
        claim["evidence"][0]["paragraph_id"] = "missing"
    elif mutation == "quote":
        claim["evidence"][0]["quote"] = "This deal has closed."
    elif mutation == "claim":
        claim["text"] = "已完成交割"
    elif mutation == "overlap":
        review["notes"][0]["claims"].append(deepcopy(claim))
    with pytest.raises(ValueError, match=error):
        audit_event(packet, response, review)


def test_removed_annotation_leaves_visible_coverage_gap(cases):
    packet, response, review = cases["PLAB"]
    review["notes"][0]["claims"].pop()
    result = audit_event(packet, response, review)
    assert any("FPD下滑" in gap["text"] for gap in result["items"][1]["unreviewed_spans"])
    assert not result["items"][1]["claim_inventory_complete"]


def test_single_claim_correct_citation_resolves_only_citation_issue(cases):
    packet, response, review = cases["PLAB"]
    evidence = review["notes"][0]["claims"][-1]["evidence"]
    claim = {"text": "FPD下滑", "evidence": [e for e in evidence if e["paragraph_id"] == "p0014"]}
    note = {"kind": "EVENT", "text": "FPD下滑。", "paragraph_ids": ["p0014"]}
    response["notes"] = [note]
    review["notes"] = [{"index": 0, "original_note": deepcopy(note), "claims": [claim]}]
    good = audit_event(packet, response, review)["items"][0]
    assert good["status"] == "REVIEW_REQUIRED" and good["claim_inventory_complete"]
    assert not good["reasons"] and not good["semantic_support_verified"]
    note["paragraph_ids"] = ["p0012"]
    review["notes"][0]["original_note"] = deepcopy(note)
    bad = audit_event(packet, response, review)["items"][0]
    assert "CLAIM_EVIDENCE_NOT_CITED" in bad["reasons"]
