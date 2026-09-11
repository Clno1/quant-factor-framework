"""Source excerpts and comparable financial observations are different products."""
from datetime import datetime
from decimal import Decimal
import re
from typing import Literal

from pydantic import Field, field_validator

from .llm_contract import StrictModel, prepare_request
from .models import digest


class FinancialObservation(StrictModel):
    observation_id: str
    issuer: str
    metric: str
    period: str
    currency: str
    basis: str
    share_basis: str
    value: str = Field(pattern=r'^-?[0-9]+(?:\.[0-9]+)?$')
    scale: Literal['UNIT', 'THOUSAND', 'MILLION', 'BILLION']
    kind: Literal['ACTUAL', 'CONSENSUS', 'COMPANY_GUIDANCE', 'COMPANY_ESTIMATE']
    observed_at: datetime
    published_at: datetime
    binding_verified: bool = False
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator('observed_at', 'published_at', mode='before')
    @classmethod
    def iso_time(cls, value):
        return datetime.fromisoformat(value) if isinstance(value, str) else value


def compare_observations(left, right, *, as_of, release_at, relation):
    """Only normalized, independently bound inputs may produce arithmetic, never LLM numbers.

    Binding approval is supplied by an upstream contract, not inferred from the
    existence of a quote. The current excerpt pipeline deliberately does not set it.
    """
    left, right = FinancialObservation.model_validate(left), FinancialObservation.model_validate(right)
    errors = []
    if relation not in {'SURPRISE', 'GUIDANCE_REVISION'}:
        raise ValueError('UNSUPPORTED_FINANCIAL_COMPARISON')
    dates = [as_of, release_at, left.observed_at, right.observed_at, left.published_at, right.published_at]
    if any(d.tzinfo is None for d in dates):
        raise ValueError('AWARE_FINANCIAL_TIMESTAMPS_REQUIRED')
    if not left.binding_verified or not right.binding_verified:
        errors.append('FINANCIAL_BINDING_NOT_VERIFIED')
    for field in ('issuer', 'metric', 'period', 'currency', 'basis', 'share_basis'):
        if getattr(left, field) != getattr(right, field) or getattr(left, field) in {'', 'UNKNOWN'}:
            errors.append(field.upper() + '_NOT_COMPARABLE')
    if max(dates[1:]) > as_of:
        errors.append('FUTURE_INFORMATION')
    if right.observed_at >= release_at or right.published_at >= release_at:
        errors.append('PRE_RELEASE_BASELINE_REQUIRED')
    expected = ('ACTUAL', 'CONSENSUS') if relation == 'SURPRISE' else ('COMPANY_GUIDANCE', 'COMPANY_GUIDANCE')
    if (left.kind, right.kind) != expected:
        errors.append('COMPARISON_VALUE_KIND_MISMATCH')
    if left.published_at < release_at:
        errors.append('CURRENT_RELEASE_OBSERVATION_REQUIRED')
    factors = {'UNIT': 1, 'THOUSAND': 1000, 'MILLION': 1000000, 'BILLION': 1000000000}
    a, b = Decimal(left.value) * factors[left.scale], Decimal(right.value) * factors[right.scale]
    result = {'relation': relation, 'input_ids': [left.observation_id, right.observation_id],
              'issuer': left.issuer, 'metric': left.metric, 'period': left.period,
              'currency': left.currency, 'basis': left.basis, 'share_basis': left.share_basis,
              'left_value': str(a), 'right_value': str(b), 'normalized_scale': 'UNIT',
              'evidence_ids': list(dict.fromkeys(left.evidence_ids + right.evidence_ids)),
              'status': 'BLOCKED' if errors else 'COMPUTED', 'blockers': errors}
    if not errors:
        result.update(difference=str(a - b), percent=str((a - b) / b * 100) if b > 0 else None,
                      percent_status='COMPUTED' if b > 0 else 'NONPOSITIVE_BASELINE_USE_ABSOLUTE_DIFFERENCE',
                      direction='ABOVE' if a > b else 'BELOW' if a < b else 'EQUAL')
    result['comparison_id'] = digest(result)
    return result


def bound_comparisons(source, bundle, *, as_of):
    """Consume a trusted upstream normalized-data contract, never an LLM response.

    An absent baseline is still absent. This adapter does not turn article text,
    title hints, management estimates or current consensus into historical data.
    """
    if bundle.get('version') != 'ep-bound-financial-observations-v1':
        raise ValueError('BOUND_FINANCIAL_INPUT_CONTRACT_REQUIRED')
    if not bundle.get('contract_revision') or not bundle.get('contract_evidence_ids'):
        raise ValueError('FINANCIAL_INPUT_PROVENANCE_REQUIRED')
    if bundle.get('source_id') != source['source_id'] or bundle.get('text_revision') != source['parsed']['text_revision']:
        raise ValueError('FINANCIAL_SOURCE_BINDING_CHANGED')
    release = datetime.fromisoformat(bundle['release_at'])
    rows = bundle.get('observations', [])
    pairs = bundle.get('pairs', [])
    if not isinstance(rows, list) or not isinstance(pairs, list) or len(rows) > 40 or len(pairs) > 10:
        raise ValueError('BOUNDED_FINANCIAL_INPUTS_REQUIRED')
    observations = [FinancialObservation.model_validate(r) for r in rows]
    indexed = {r.observation_id: r for r in observations}
    if len(indexed) != len(rows) or any(r.issuer != source['ticker'] for r in observations):
        raise ValueError('FINANCIAL_ISSUER_OR_ID_CONFLICT')
    computed, rejected, seen = [], [], set()
    for pair in pairs:
        try:
            left, right = indexed[pair['left']], indexed[pair['right']]
            key = (pair['relation'], pair['left'], pair['right'])
            if key in seen:
                raise ValueError('DUPLICATE_COMPARISON')
            seen.add(key)
            result = compare_observations(left, right, as_of=as_of, release_at=release, relation=pair['relation'])
            if result['status'] == 'COMPUTED':
                computed.append(result)
            else:
                rejected.append(result)
        except (ValueError, KeyError, TypeError):
            rejected.append({'status': 'BLOCKED', 'blockers': ['INVALID_OR_DUPLICATE_COMPARISON_INPUT']})
    return {'comparisons': computed, 'rejected_comparisons': rejected,
            'comparison_blockers': sorted({b for r in rejected for b in r['blockers']}) or (
                [] if computed else ['NO_BOUND_ASOF_COMPARABLE_BASELINE']),
            'input_revision': digest(bundle), 'contract_revision': bundle['contract_revision'],
            'semantics': 'PROGRAM_ARITHMETIC_ON_UPSTREAM_BOUND_INPUTS_NOT_LLM_FACT_APPROVAL'}


def enrich_report(store, report, path, *, as_of):
    """Attach deterministic comparisons without changing archived model packets."""
    from copy import deepcopy
    import json
    from pathlib import Path
    if not path:
        return report
    result = deepcopy(report)
    sections = result.setdefault('sections', {})
    try:
        path = Path(path)
        if path.stat().st_size > 2_000_000:
            raise ValueError('FINANCIAL_INPUT_TOO_LARGE')
        manifest = json.loads(path.read_text())
        if manifest.get('version') != 'ep-financial-input-manifest-v1':
            raise ValueError('FINANCIAL_MANIFEST_REQUIRED')
        bundles = manifest.get('sources')
        if not isinstance(bundles, list) or len(bundles) > 100:
            raise ValueError('BOUNDED_SOURCE_MANIFEST_REQUIRED')
        selected = [b for b in bundles if b.get('source_id') == report['source_id']]
        if len(selected) != 1:
            raise ValueError('UNIQUE_FINANCIAL_SOURCE_INPUT_REQUIRED')
        source = store.source_detail(report['source_id'], as_of=as_of)
        data = bound_comparisons(source, selected[0], as_of=as_of)
        sections.update(program_comparisons=data['comparisons'], comparison_blockers=data['comparison_blockers'],
                        rejected_comparisons=data['rejected_comparisons'])
        result['finance_input_revision'] = digest([data['input_revision'], data['comparisons']])
    except (ValueError, KeyError, TypeError, OSError):
        sections.update(program_comparisons=[], comparison_blockers=['BOUND_FINANCIAL_INPUT_UNAVAILABLE_OR_INVALID'])
    return result


def financial_context(source, paragraph_ids):
    prepared = prepare_request(source)
    selected = [p for p in prepared['untrusted_paragraphs'] if p['id'] in paragraph_ids]
    disclosures = []
    for p in selected:
        if not re.search(r'revenue|net sales|EPS|per .*share|guidance|outlook|tax benefit|refund|one.time|ARR|cRPO|backlog|retention|GMV', p['text'], re.I):
            continue
        disclosures.append({'disclosure_id': 'd' + digest([prepared['text_revision'], p])[:24],
            'paragraph_id': p['id'], 'quote': p['text'], 'source_url': source['result']['final_url'],
            'status': 'VERBATIM_DISCLOSURE_NOT_NORMALIZED_FACT', 'financial_binding_verified': False})
    return {'version': 'ep-financial-context-v1', 'disclosures': disclosures, 'comparisons': [],
            'comparison_blockers': ['NO_BOUND_ASOF_COMPARABLE_BASELINE'],
            'coverage': prepared['coverage'], 'financial_semantics_verified': False}


def select_context_paragraphs(source, *, limit=10):
    paragraphs = prepare_request(source)['untrusted_paragraphs']
    # Preserve introduction, then whole relevant paragraphs. Never splice table cells.
    ordered = paragraphs[:2] + [p for p in paragraphs[2:] if re.search(
        r'revenue|EPS|per .*share|guidance|outlook|tax|refund|backlog|ARR|acquisition|agreement|contract', p['text'], re.I)]
    selected, size = [], 0
    for p in ordered:
        if len(selected) == limit:
            break
        if len(p['text']) > 4000 or size + len(p['text']) > 12000:
            continue
        selected.append(p['id'])
        size += len(p['text'])
    return selected or [paragraphs[0]['id']]
