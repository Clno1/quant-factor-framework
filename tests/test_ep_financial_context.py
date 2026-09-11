from copy import deepcopy
from datetime import timedelta
import json

import pytest

from src.breakouts.ep.financial_context import compare_observations, financial_context, select_context_paragraphs
from src.breakouts.ep.llm_event_context import prepare_context_packet, validate_context
from src.breakouts.ep.event_worker import cycle
from test_ep_llm import seeded, CLOCK, evidence_source
from test_ep_sources import observed
from test_ep_event_worker import config


def answer(request):
    return {**{k: request[k] for k in ('request_id', 'document_id', 'text_revision')},
            'scope_status': 'UNCERTAIN', 'disclosure_ids': [d['disclosure_id'] for d in request['financial_context']['disclosures'][:1]],
            'notes': [{'kind': 'INTERPRETATION', 'text': '营收表现可能有助于理解公司的业务变化。',
                       'paragraph_ids': [request['untrusted_blocks'][0]['paragraph_id']]}]}


def test_financial_terms_allowed_but_numbers_materialized_from_ids():
    source = evidence_source()
    packet = prepare_context_packet(source, ['p0002'])
    context = financial_context(source, ['p0002'])
    assert context['disclosures'] and not context['comparisons']
    raw = answer(packet['request'])
    result = validate_context(packet, raw)
    assert not result['rejected']
    assert result['accepted'][0]['kind'] == 'SOURCE_DISCLOSURE'
    assert result['accepted'][0]['evidence'][0]['quote'] == context['disclosures'][0]['quote']
    assert not result['semantic_support_verified']
    raw['notes'][0]['text'] = '营收为 $928 million，可能代表增长。'
    assert 'NUMERIC_INFORMATION_MUST_USE_DISCLOSURE_IDS' in validate_context(packet, raw)['rejected'][0]['reasons']
    raw['notes'][0]['text'] = '营收可能超预期。'
    assert 'COMPARISON_OR_TRADING_ASSERTION_NOT_ALLOWED' in validate_context(packet, raw)['rejected'][0]['reasons']
    raw['disclosure_ids'] = ['invented']
    assert validate_context(packet, raw)['rejected'][0]['disclosure_id'] == 'invented'
    assert select_context_paragraphs(source)


def test_past_financial_explanation_cannot_be_attributed_as_management_forecast():
    source = evidence_source()
    packet = prepare_context_packet(source, ['p0002'])
    from src.breakouts.ep.models import digest
    packet['request']['untrusted_blocks'][0]['text'] = (
        'Under IFRS accounting rules, these cash rewards are deducted from revenue rather than recorded as a cost.')
    packet['packet_hash'] = digest({'request': packet['request']})
    raw = answer(packet['request'])
    raw['disclosure_ids'] = []
    raw['notes'] = [{'kind': 'MANAGEMENT_EXPECTATION',
        'text': '公司预计该策略可能持续影响报告收入与总交易价值之间的呈现差异。', 'paragraph_ids': ['p0002']}]
    result = validate_context(packet, raw)
    assert not result['accepted']
    assert 'FORWARD_LOOKING_SOURCE_EVIDENCE_REQUIRED' in result['rejected'][0]['reasons']


class ContextTransport:
    def __init__(self):
        self.calls = 0

    def generate(self, payload):
        self.calls += 1
        request = json.loads(payload['messages'][1]['content'])
        assert 'disclosure_ids' in payload['response_format']['json_schema']['schema']['properties']
        return {'object': 'chat.completion', 'model': 'kimi-k2.6', 'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': json.dumps(answer(request))}}], 'usage': {'prompt_tokens': 500, 'completion_tokens': 200}}


def test_context_protocol_uses_existing_budget_dedup_and_archives(tmp_path, observed):
    store, _, sid = seeded(observed)
    cfg = config(tmp_path, store, sid).model_copy(update={'analysis_protocol': 'event-context'})
    transport = ContextTransport()
    before = cycle(cfg, clock=CLOCK, key_reader=lambda _: pytest.fail('plan reads key'))
    assert before['external_requests'] == 0
    done = cycle(cfg, execute=True, clock=CLOCK, transport_factory=lambda *a: transport, key_reader=lambda _: 'fake')
    assert 'report' in done['items'][0], done
    report = done['items'][0]['report']
    assert report['sections']['source_disclosures'] and not report['sections']['program_comparisons']
    assert report['protocol'] == 'event-context'
    again = cycle(cfg, execute=True, clock=CLOCK, key_reader=lambda _: pytest.fail('dedup reads key'))
    assert again['external_requests'] == 0 and transport.calls == 1
    assert again['budget_after'] == done['budget_after']


def observation(**kw):
    now = CLOCK()
    return dict(observation_id='o1', issuer='SNOW', metric='REVENUE', period='FY2027Q2', currency='USD',
                basis='GAAP', share_basis='NOT_APPLICABLE', value='1.55', scale='BILLION', kind='ACTUAL',
                observed_at=now, published_at=now, binding_verified=True, evidence_ids=['source:p1'], **kw)


def compare(left, right):
    return compare_observations(left, right, as_of=CLOCK(), release_at=CLOCK(), relation='SURPRISE')


def pair():
    left = observation()
    right = {**left, 'observation_id': 'o2', 'value': '1490', 'scale': 'MILLION', 'kind': 'CONSENSUS',
             'observed_at': CLOCK() - timedelta(hours=1), 'published_at': CLOCK() - timedelta(hours=1)}
    return left, right


def test_comparable_units_and_nonpositive_baseline():
    left, right = pair()
    result = compare(left, right)
    assert result['status'] == 'COMPUTED' and result['difference'] == '60000000.00'
    right['value'] = '-1490'
    assert compare(left, right)['percent'] is None
    right['value'] = '0'
    assert compare(left, right)['percent'] is None


@pytest.mark.parametrize('changes,reason', [
    ({'period': 'FY2026Q2'}, 'PERIOD_NOT_COMPARABLE'),
    ({'currency': 'UNKNOWN'}, 'CURRENCY_NOT_COMPARABLE'),
    ({'basis': 'NON_GAAP'}, 'BASIS_NOT_COMPARABLE'),
    ({'kind': 'COMPANY_ESTIMATE'}, 'COMPARISON_VALUE_KIND_MISMATCH'),
    ({'binding_verified': False}, 'FINANCIAL_BINDING_NOT_VERIFIED'),
    ({'observed_at': CLOCK()}, 'PRE_RELEASE_BASELINE_REQUIRED'),
    ({'observed_at': CLOCK() + timedelta(hours=1)}, 'FUTURE_INFORMATION'),
])
def test_comparisons_fail_closed(changes, reason):
    left, right = pair()
    right.update(changes)
    result = compare(left, right)
    assert result['status'] == 'BLOCKED' and reason in result['blockers']
    assert 'percent' not in result


def bound_bundle(source):
    left, right = pair()
    for row in (left, right):
        row['issuer'] = source['ticker']
        row['observed_at'] = row['observed_at'].isoformat()
        row['published_at'] = row['published_at'].isoformat()
    return {'version': 'ep-bound-financial-observations-v1', 'contract_revision': 'offline-test-contract',
        'contract_evidence_ids': ['fixture-only-not-a-live-consensus-feed'],
        'source_id': source['source_id'], 'text_revision': source['parsed']['text_revision'],
        'release_at': CLOCK().isoformat(), 'observations': [left, right],
        'pairs': [{'relation': 'SURPRISE', 'left': 'o1', 'right': 'o2'}]}


def test_bound_financial_inputs_are_explicit_not_llm_numbers():
    from src.breakouts.ep.financial_context import bound_comparisons
    src = evidence_source()
    bundle = bound_bundle(src)
    result = bound_comparisons(src, bundle, as_of=CLOCK())
    assert len(result['comparisons']) == 1 and result['comparison_blockers'] == []
    bundle['observations'][1]['kind'] = 'ACTUAL'
    rejected = bound_comparisons(src, bundle, as_of=CLOCK())
    assert not rejected['comparisons']
    assert 'COMPARISON_VALUE_KIND_MISMATCH' in rejected['comparison_blockers']
    bundle['text_revision'] = 'changed'
    with pytest.raises(ValueError, match='SOURCE_BINDING_CHANGED'):
        bound_comparisons(src, bundle, as_of=CLOCK())


def test_guidance_revision_and_missing_currency_do_not_become_beat():
    from src.breakouts.ep.financial_context import bound_comparisons
    src = evidence_source()
    bundle = bound_bundle(src)
    for row in bundle['observations']:
        row['kind'] = 'COMPANY_GUIDANCE'
    bundle['pairs'][0]['relation'] = 'GUIDANCE_REVISION'
    result = bound_comparisons(src, bundle, as_of=CLOCK())
    assert result['comparisons'][0]['relation'] == 'GUIDANCE_REVISION'
    bundle['observations'][1]['currency'] = 'UNKNOWN'
    assert not bound_comparisons(src, bundle, as_of=CLOCK())['comparisons']


def test_financial_input_enrichment_delivery_dedup_without_llm(tmp_path, observed):
    from src.breakouts.ep.financial_context import enrich_report
    from src.alerts.ep_event import EventOutbox, ai_payload
    from test_ep_event_delivery import report, Sender
    store, _, sid = seeded(observed)
    src = store.source_detail(sid)
    bundle = bound_bundle(src)
    path = tmp_path / 'financial.json'
    path.write_text(json.dumps({'version': 'ep-financial-input-manifest-v1', 'sources': [bundle]}))
    base = {**report(), 'source_id': sid, 'ticker': src['ticker'], 'protocol': 'event-context'}
    enriched = enrich_report(store, base, path, as_of=CLOCK())
    assert enriched['sections']['program_comparisons']
    payload, _ = ai_payload(enriched, style='personal')
    assert '独立程序比较展示 1/1' in payload['content']
    assert '缺少已对齐' not in payload['content']
    assert any('程序财务比较' in e['title'] for e in payload['embeds'])
    outbox = EventOutbox(tmp_path / 'outbox.db')
    first = outbox.enqueue(enriched, 'route', now=100, style='personal')
    assert outbox.enqueue(enriched, 'route', now=101, style='personal') == first
    sender = Sender()
    assert outbox.deliver(sender, 'route', now=102, style='personal', report_loader=lambda _: enriched)[0]['state'] == 'SENT'
    bundle['observations'][1]['value'] = '1400'
    path.write_text(json.dumps({'version': 'ep-financial-input-manifest-v1', 'sources': [bundle]}))
    revised = enrich_report(store, base, path, as_of=CLOCK())
    assert outbox.enqueue(revised, 'route', now=103, style='personal') != first
    assert len(sender.calls) == 1  # No bypass of existing delivery cooldown.
    bundle['observations'][1]['observed_at'] = (CLOCK() + timedelta(hours=1)).isoformat()
    path.write_text(json.dumps({'version': 'ep-financial-input-manifest-v1', 'sources': [bundle]}))
    assert not enrich_report(store, base, path, as_of=CLOCK())['sections']['program_comparisons']
