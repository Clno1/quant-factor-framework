"""Fixed output scopes; input coverage and financial completeness remain separate."""
from copy import deepcopy
import json

VERSION = "ep-llm-batches-v3"
SCOPES = {
    "revenue": {"metrics": ["REVENUE"], "value_kinds": ["ACTUAL"],
        "focus": "Reported issuer revenue/net sales. Prefer the latest reported period, then its comparable prior period. Exclude guidance."},
    "eps": {"metrics": ["EPS"], "value_kinds": ["ACTUAL"],
        "focus": "Reported issuer EPS. Prefer latest-period GAAP and non-GAAP diluted EPS. Keep accounting bases separate. Exclude guidance."},
    "guidance": {"metrics": ["REVENUE", "EPS", "OPERATING_MARGIN"], "value_kinds": ["COMPANY_GUIDANCE"],
        "focus": "Explicit current issuer revenue/EPS/operating-margin outlook. Prefer next quarter and full year. Preserve ranges verbatim. Do not infer a raise or beat."},
    "special_items": {"metrics": ["SPECIAL_ITEM"], "value_kinds": ["SPECIAL_ITEM_IMPACT", "ACTUAL"],
        "focus": "Explicit quantified exceptional tax benefits, refunds, gains or charges. Do not classify routine compensation/amortization as one-time just because excluded from non-GAAP. No unsupported absence claims."},
}


def batch_spec(name):
    if name not in SCOPES:
        raise ValueError("UNKNOWN_LLM_BATCH")
    return {"version": VERSION, "name": name, "claim_limit": 4, "quote_char_limit": 1200,
            "citations_per_claim": 4, "context_evidence_version": "ep-field-evidence-v1",
            **deepcopy(SCOPES[name])}


def batch_rules(spec):
    return (
        " Return a complete JSON object for this batch only: " + spec["focus"] +
        " At most 4 claims. Each claim has at most 4 citations, each at most 1200 characters; "
        "retain enough verbatim context to interpret the number. Never silently cut an essential table header. "
        "Use only the batch metrics and value_kinds. Every *_text field is a verbatim source span or null "
        "where nullable: do not write USD when only $ appears, actual result when it is not printed, "
        "or a normalized fiscal period instead of the original wording. Preserve spaces inside numbers. "
        "Use scope_status=MORE_FACTS_REMAIN if other in-scope facts remain after this batch's cap; "
        "UNCERTAIN if completeness or necessary context cannot be determined; otherwise COMPLETE_FOR_SCOPE. "
        "This is a self-report, not proof of exhaustiveness. Do not discuss out-of-scope metrics. "
        "The enum fields and *_text fields are DIFFERENT: value_kind=ACTUAL does NOT mean "
        "value_kind_text=ACTUAL. If the literal label is absent, value_kind_text must be null. "
        "If basis_text is null, use basis=UNKNOWN, not GAAP. A revenue amount has "
        "share_basis=NOT_APPLICABLE, not BASIC. subject_text is an entity name, NEVER a fiscal period. "
        "Use only citations needed to support this one value and its context, not repeated copies "
        "from multiple tables. A period found in a table header needs a citation to that header. "
        "Additionally provide context_evidence (at most 6 entries, quote at most 400 characters): "
        "each entry contains role (SUBJECT, PERIOD, CURRENCY, SCALE), text, paragraph_id, quote. "
        "These are separate from the value-row evidence. For each non-null subject_text, period_text, "
        "unit_text and unit_scale_text, include a matching role entry whose text equals that field "
        "EXACTLY and whose quote contains it verbatim. Find the company name in the relevant issuer "
        "heading or sentence; never substitute a subsidiary, segment or acquisition target. "
        "Keep currency ($) separate from scale (in millions, in thousands). unit_scale_text is null "
        "when unknown or inapplicable; for EPS do not apply a table's millions scale if per-share data "
        "are excepted. Quote unit exceptions in full. If period headers are split across rows, set "
        "period_text=null and cite each exact fragment as separate PERIOD entries; never concatenate "
        "a synthetic period. Do not borrow a header from another table. These anchors locate text, "
        "NOT prove issuer, table-column, period or unit applicability; omit uncertain context rather "
        "than guessing. Do not insert citations not present in the supplied input. "
        "Here are synthetic format examples, NOT facts from the supplied source. Never output their "
        "values or example IDs; use only untrusted_paragraphs from this request: " + json.dumps(format_examples())
    )


def format_examples():
    paragraphs = [
        {"id": "example-revenue", "text": "Example Inc. reported revenue of $120 million."},
        {"id": "example-header", "text": "Q2 FY 2027"},
        {"id": "example-eps", "text": "Non-GAAP diluted earnings per share $ 0.24"},
    ]
    common = {"basis_text": None, "share_basis_text": None, "value_kind_text": None, "unit_text": "$",
              "basis": "UNKNOWN", "share_basis": "NOT_APPLICABLE", "value_kind": "ACTUAL", "subject_scope": "UNKNOWN"}
    result = {"paragraphs": paragraphs, "valid_claims": [
        {**common, "metric": "REVENUE", "metric_text": "revenue", "value_text": "$120 million",
         "subject_text": "Example Inc.", "period_text": None, "value_kind_text": "reported",
         "evidence": [{"paragraph_id": "example-revenue", "quote": paragraphs[0]["text"]}]},
        {**common, "metric": "EPS", "metric_text": "earnings per share", "value_text": "$ 0.24",
         "subject_text": None, "period_text": "Q2 FY 2027", "basis_text": "Non-GAAP", "share_basis_text": "diluted",
         "basis": "NON_GAAP", "share_basis": "DILUTED", "evidence": [
             {"paragraph_id": "example-header", "quote": paragraphs[1]["text"]},
             {"paragraph_id": "example-eps", "quote": paragraphs[2]["text"]}]},
    ]}
    for claim in result["valid_claims"]:
        claim["unit_scale_text"] = None
        claim["context_evidence"] = []
        for role, field in (("SUBJECT", "subject_text"), ("PERIOD", "period_text"), ("CURRENCY", "unit_text")):
            if claim[field] is not None:
                citation = next(e for e in claim["evidence"] if claim[field] in e["quote"])
                claim["context_evidence"].append({"role": role, "text": claim[field], **citation})
    return result


def scoped_schema(schema, spec):
    schema = deepcopy(schema)
    schema["properties"]["claims"]["maxItems"] = spec["claim_limit"]
    schema["$defs"]["Claim"]["properties"]["evidence"]["maxItems"] = spec["citations_per_claim"]
    schema["$defs"]["Citation"]["properties"]["quote"]["maxLength"] = spec["quote_char_limit"]
    schema["$defs"]["Claim"]["properties"]["metric"]["enum"] = spec["metrics"]
    schema["$defs"]["Claim"]["properties"]["value_kind"]["enum"] = spec["value_kinds"]
    schema["properties"]["scope_status"] = {"type": "string", "enum": [
        "COMPLETE_FOR_SCOPE", "MORE_FACTS_REMAIN", "UNCERTAIN"]}
    schema["required"].append("scope_status")
    return schema
