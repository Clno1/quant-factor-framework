"""Human-reviewed references, NOT model output or automatic correction rules."""


def ref(paragraph, text, occurrence=0):
    return {"paragraph_id": paragraph, "text": text, "occurrence": occurrence}


def common(metric, kind="ACTUAL"):
    return dict(metric=metric, basis="UNKNOWN", share_basis="NOT_APPLICABLE",
                value_kind=kind, subject_scope="ISSUER", basis_span=None,
                share_basis_span=None, value_kind_span=None, unit_scale_span=None)


def reference_cases():
    cases = []
    for index, (value, period) in enumerate((("$ 286.3", "Q2 FY 2027"), ("$ 236.0", "Q2 FY 2026"))):
        cases.append(dict(ticker="GTLB", original_index=index, selection={**common("REVENUE"),
            "metric_span": ref("p0020", "Revenue"), "value_span": ref("p0020", value),
            "subject_span": ref("p0007", "GitLab Inc."), "period_spans": [ref("p0019", period)],
            "unit_span": ref("p0020", "$", index), "unit_scale_span": ref("p0018", "in millions")}))
    for index, (value, year) in enumerate((("286,254", "2026"), ("235,960", "2025")), 2):
        cases.append(dict(ticker="GTLB", original_index=index, selection={**common("REVENUE"),
            "metric_span": ref("p0136", "Total revenue"), "value_span": ref("p0136", value),
            "subject_span": ref("p0127", "GitLab Inc."), "period_spans": [
                ref("p0131", "Three Months Ended July 31,"), ref("p0132", year)],
            "unit_span": None, "unit_scale_span": ref("p0129", "in thousands")}))
    for index, value in enumerate(("$100 million", "$1.75")):
        cases.append(dict(ticker="ANF", original_index=index, selection={**common("SPECIAL_ITEM", "SPECIAL_ITEM_IMPACT"),
            "metric_span": ref("p0005", "IEEPA tariff refund benefit"), "value_span": ref("p0005", value),
            "subject_span": ref("p0009", "Abercrombie & Fitch Co."),
            "period_spans": [ref("p0009", "second quarter ended August 1, 2026")],
            "unit_span": ref("p0005", "$", index + 1),
            "unit_scale_span": ref("p0005", "million") if index == 0 else None,
            "share_basis": "NOT_APPLICABLE" if index == 0 else "DILUTED",
            "share_basis_span": None if index == 0 else ref("p0005", "diluted", 1)}))
    for index, (row, subject, period, scale, period_text) in enumerate((
        ("p0182", "p0176", "p0178", "p0179", "Thirteen Weeks Ended August 2, 2025"),
        ("p0199", "p0193", "p0195", "p0196", "Twenty-Six Weeks Ended August 2, 2025")), 2):
        cases.append(dict(ticker="ANF", original_index=index, selection={**common("SPECIAL_ITEM", "SPECIAL_ITEM_IMPACT"),
            "metric_span": ref(row, "Litigation settlement"), "value_span": ref(row, "$ (38,574)"),
            "subject_span": ref(subject, "Abercrombie & Fitch Co."), "period_spans": [ref(period, period_text)],
            "unit_span": ref(row, "$"), "unit_scale_span": ref(scale, "in thousands")}))
    # The old table/prose proposals describe duplicate EPS values. Keep all four
    # failure cases, but explicitly report duplicates rather than four new facts.
    for index in range(4):
        row, value, basis = ("p0008", "$0.49", "GAAP") if index < 2 else ("p0010", "$0.50", "Non-GAAP")
        cases.append(dict(ticker="PLAB", original_index=index, selection={**common("EPS"),
            "metric_span": ref(row, "per diluted share"), "value_span": ref(row, value),
            "subject_span": ref(row, "Photronics, Inc."), "period_spans": [
                ref("p0002", "third quarter of fiscal year 2026 ended August 2, 2026")],
            "unit_span": ref(row, "$", 1), "basis": "GAAP" if index < 2 else "NON_GAAP",
            "basis_span": ref(row, basis), "share_basis": "DILUTED", "share_basis_span": ref(row, "diluted")}))
    return cases


def select_reference(packet, annotation):
    """Test-only oracle: annotations specify exact source text, not fuzzy repairs."""
    if annotation is None:
        return None
    block = next(b for b in packet["request"]["untrusted_blocks"] if b["paragraph_id"] == annotation["paragraph_id"])
    start, position = -1, 0
    for _ in range(annotation["occurrence"] + 1):
        start = block["text"].find(annotation["text"], position)
        if start == -1:
            raise ValueError("REFERENCE_TEXT_NOT_FOUND")
        position = start + 1
    end = start + len(annotation["text"])
    starts = [f for f in block["fragments"] if f["start"] == start and f["end"] <= end]
    ends = [f for f in block["fragments"] if f["end"] == end and f["start"] >= start]
    if not starts or not ends:
        raise ValueError("REFERENCE_NOT_ON_FRAGMENT_BOUNDARIES")
    return {"start_id": max(starts, key=lambda f: f["end"])["id"],
            "end_id": min(ends, key=lambda f: f["start"])["id"]}


def selected_response(packet, selection):
    value = {k: packet["request"][k] for k in ("request_id", "document_id", "text_revision")}
    spans = {k: select_reference(packet, v) for k, v in selection.items() if k.endswith("_span")}
    periods = [select_reference(packet, v) for v in selection["period_spans"]]
    return {**value, "scope_status": "UNCERTAIN", "selections": [
        {**selection, **spans, "period_spans": periods}]}
