from collections import Counter
from datetime import timedelta

import pytest

from src.breakouts.ep.classifier import classify, routing_hint
from src.breakouts.ep.event_identity import event_descriptor
from src.breakouts.ep.pipeline import select_source_jobs, source_plan, process_identities, seed
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.store import EpStore
from src.breakouts.ep.identity import IdentitySnapshot
from src.breakouts.ep.models import EpSettings
from src.breakouts.ep.service import EpRadar
from test_ep_event_queue_watch import NOW, payload
from test_ep_pipeline import worker
from test_ep_radar import FakeProvider, article, NOW as NEWS_NOW


@pytest.mark.parametrize('title,hint', [
    ('Hsbc Holdings PLC Acquires 22,500 Shares of Fluor Corporation $FLR', 'OWNERSHIP_UPDATE'),
    ('Copart acquires ACV Auctions for cash', 'M_AND_A'),
    ('Solaris acquires a stake in Deployable Energy', 'M_AND_A'),
    ('Oracle Q1 Earnings Beat Estimates, Cloud Growth Accelerates', 'EARNINGS'),
    ('RH Q2 Earnings Call Highlights', 'EARNINGS'),
    ('RH to Report Second Quarter Fiscal 2026 Financial Results', 'EARNINGS_PREVIEW'),
    ('Oracle Sets the Date for its First Quarter Fiscal 2027 Earnings Announcement', 'EARNINGS_PREVIEW'),
    ("Dave & Buster's Expectations Ahead Of Q2 Earnings", 'EARNINGS_PREVIEW'),
])
def test_headlines_are_routing_hints_not_verified_catalysts(title, hint):
    event = payload('d', title)['event']
    result = classify({'kind': 'ARTICLE', 'payload': event['evidence'], **event, 'first_seen_at': NOW.isoformat()})
    assert result['event_type_hint'] == hint
    assert result['relation'] == 'UNVERIFIED' and result['confidence'] == 'HEADLINE_ONLY'


def test_reclassify_existing_jobs_without_rewriting_evidence():
    event = payload('d', 'Hsbc Holdings PLC Acquires 22,500 Shares of Fluor', hint='M_AND_A')['event']
    assert routing_hint(event) == 'OWNERSHIP_UPDATE'
    assert event['event_type_hint'] == 'M_AND_A'
    preview = payload('p', 'RH to Report Second Quarter Fiscal 2026 Financial Results', hint='EARNINGS_PREVIEW')['event']
    assert event_descriptor('RH', preview)['event_type'] == 'EARNINGS_PREVIEW'
    assert event_descriptor('RH', preview)['event_id'] != event_descriptor('RH', payload('r')['event'])['event_id']


def test_diverse_issuers_then_second_distinct_event(tmp_path):
    queue = PipelineQueue(tmp_path / 'q.db')
    for i in range(20):
        queue.enqueue('SOURCE', 'RH', str(i), 'v1', payload(str(i)), NOW, NOW + timedelta(hours=2))
    for symbol in ['ORCL', 'CPRT', 'SNOW', 'PLAB', 'GTLB', 'ANF', 'SAIC']:
        queue.enqueue('SOURCE', symbol, symbol, 'v1', payload(symbol), NOW, NOW + timedelta(hours=2))
    selected = select_source_jobs(queue, NOW, 8)
    assert len(selected) == 8 and len({r['ticker'] for r in selected}) == 8
    single = PipelineQueue(tmp_path / 'single.db')
    for i in range(10):
        single.enqueue('SOURCE', 'RH', str(i), 'v1', payload(str(i)), NOW, NOW + timedelta(hours=2))
    assert len(select_source_jobs(single, NOW, 8)) == 2
    plan = source_plan(PipelineQueue(queue.path, read_only=True), NOW, 8)
    assert len(plan['selected']) == 8 and plan['external_requests'] == 0
    assert not queue.checkpoint('source:selection')


def test_source_selection_excludes_future_and_old_publications(tmp_path):
    queue = PipelineQueue(tmp_path / 'q.db')
    for symbol, published in [('OLD', NOW - timedelta(days=3)), ('FUTURE', NOW + timedelta(minutes=1)), ('CURRENT', NOW)]:
        row = payload(symbol)
        row['event']['published_at'] = published.isoformat()
        queue.enqueue('SOURCE', symbol, symbol, 'v1', row, NOW, NOW + timedelta(hours=2))
    assert [r['ticker'] for r in select_source_jobs(queue, NOW, 8)] == ['CURRENT']


def test_one_negative_profile_request_per_issuer_not_per_article(tmp_path):
    store = EpStore(tmp_path / 'e.db')
    queue = PipelineQueue(tmp_path / 'q.db')
    provider = FakeProvider(articles=[article('MISS', url=f'https://example.test/{i}') for i in range(4)],
                            calendar=[], profiles={'MISS': None})
    report = EpRadar(store, provider, EpSettings(max_profiles=0), clock=lambda: NEWS_NOW).collect('2026-09-02', '2026-09-03')
    seed(queue, report, NEWS_NOW)
    cfg = worker(tmp_path, store, identity_fallback_requests=20)
    first = process_identities(queue, store, provider, IdentitySnapshot(NEWS_NOW), cfg, clock=lambda: NEWS_NOW)
    assert first['http_requests'] == 1
    assert first['counts']['RECENT_IDENTITY_FAILURE_CACHED'] == 3
    at = NEWS_NOW + timedelta(minutes=16)
    second = process_identities(queue, store, provider, IdentitySnapshot(at), cfg, clock=lambda: at)
    assert second['http_requests'] == 1
    assert Counter(c[0] for c in provider.calls)['profile'] == 2
