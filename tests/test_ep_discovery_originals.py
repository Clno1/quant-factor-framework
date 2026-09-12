from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.breakouts.ep.classifier import classify
from src.breakouts.ep.discovery import OfficialSourceDiscovery
from src.breakouts.ep.models import CatalystSnapshot
from src.breakouts.ep.source_verifier import verify_sec_event
from src.breakouts.ep.sec_source import parse_sec_attachment
from src.breakouts.ep.store import EpStore
from src.breakouts.ep.pipeline import process_sources
from src.breakouts.ep.queue import PipelineQueue
from src.breakouts.ep.llm_contract import prepare_request
from test_ep_peer_regressions import ArchivedClient, decode

BUNDLE = Path(__file__).parent / 'fixtures/ep_discovery_sources_20260912.json'


def case(symbol):
    return next(c for c in json.loads(BUNDLE.read_text())['cases'] if c['ticker'] == symbol)


def oracle():
    row = case('ORCL')
    fetch = next(f for f in row['fetches'] if f['url'].endswith('orcl-ex99_1.htm'))
    return {'evidence': json.loads(row['document']['payload_json'])}, parse_sec_attachment(decode(fetch), fetch['url'])


def test_real_oracle_release_uses_explicit_issuer_period_paragraph():
    event, parsed = oracle()
    result = verify_sec_event(event, parsed, {}, 'ORCL', '')
    assert result['status'] == 'DOCUMENT_MATCHED'
    assert result['period_evidence'] == [{'paragraph_id': 'p0014', 'fiscal_period': ['2027', '1']}]
    assert not result['financial_facts_verified'] and not result['historical_availability_verified']


@pytest.mark.parametrize('text', [
    'Oracle (NYSE: ORCL) expects Q1 FY27 results to increase next year.',
    'Oracle (NYSE: ORCL) announced it will report Q1 FY27 results next week.',
    'Oracle (NYSE: ORCL) previously announced Q1 FY27 results.',
    'Other company (NYSE: WRONG) today announced Q1 FY27 results.',
    'Oracle (NYSE: ORCL) today announced Q2 FY27 results.',
    'Oracle (NYSE: ORCL) today announced Q1 FY26 results.',
])
def test_future_prior_and_other_issuer_statements_do_not_bind_period(text):
    event, parsed = oracle()
    parsed['paragraphs'][13]['text'] = text
    assert verify_sec_event(event, parsed, {}, 'ORCL', '')['status'] == 'DOCUMENT_UNVERIFIED'


@pytest.mark.parametrize('title', ['Oracle Q1 FY2026 Results', 'Oracle Announces Q2 Results'])
def test_conflicting_title_and_release_period_is_rejected(title):
    event, parsed = oracle()
    parsed['title'] = title
    result = verify_sec_event(event, parsed, {}, 'ORCL', '')
    assert result['status'] == 'DOCUMENT_UNVERIFIED' and result['period_conflict']


@pytest.mark.parametrize('symbol,expected', [
    ('ORCL', 'DOCUMENT_MATCHED'), ('MNY', 'DOCUMENT_MATCHED'),
    ('ADBE', 'NO_MATCHING_EXHIBIT_IN_FETCHED_SCOPE'),
    ('ZUMZ', 'NO_MATCHING_EXHIBIT_IN_FETCHED_SCOPE'),
    ('HOFT', 'NO_MATCHING_EXHIBIT_IN_FETCHED_SCOPE'),
    ('DAVE', 'NO_SUPPORTED_RECENT_FILING_IN_WINDOW'),
    ('SHIP', 'NO_SUPPORTED_RECENT_FILING_IN_WINDOW'),
    ('FOXA', 'NO_SUPPORTED_RECENT_FILING_IN_WINDOW'),
])
def test_full_archived_sec_chain_without_network_or_llm(tmp_path, symbol, expected):
    row = case(symbol)
    doc = row['document']
    at = datetime.fromisoformat(row['observed_at']) + timedelta(minutes=1)
    store = EpStore(tmp_path / 'e.db')
    payload = json.loads(doc['payload_json'])
    event = classify({**doc, 'payload': payload})
    run = store.start_run(doc['event_date'], doc['event_date'], {}, at)
    store.save_page(run, {}, [CatalystSnapshot(doc['document_id'], doc['revision_id'], symbol,
        doc['kind'], doc['event_date'], doc['published_at'], doc['first_seen_at'], payload)])
    entry = row['result']['registry_entry']
    client = ArchivedClient(row, at)
    resolver = OfficialSourceDiscovery(store, client, {'version': 'v1', 'issuers': {symbol: entry}}, clock=lambda: at)
    result = resolver.resolve_event(run, {'ticker': symbol, 'identity': {'name': entry['name']}}, event)
    assert result['status'] == expected, result
    if expected != 'DOCUMENT_MATCHED':
        return
    assert store.source_detail(result['source_id'])['parsed']['paragraphs']
    store.save_profile(run, symbol, at, 'OK', {'ticker': symbol, 'name': entry['name'], 'cik': entry['cik'],
        'asset_type': 'STOCK', 'exchange': 'NASDAQ', 'is_actively_trading': True})
    queue = PipelineQueue(tmp_path / 'q.db')
    queue.enqueue_event(symbol, {'run_id': run, 'event': event}, at, at + timedelta(hours=1))
    assert process_sources(queue, store, resolver, SimpleNamespace(source_jobs_per_cycle=1),
                           clock=lambda: at) == {'DOCUMENT_MATCHED': 1}
    analysis = queue.ready('ANALYSIS', at)
    assert len(analysis) == 1
    assert prepare_request(store.source_detail(analysis[0]['payload']['source_id']))['untrusted_paragraphs']


def test_no_year_in_news_does_not_invent_one_from_today():
    event, parsed = oracle()
    event['evidence'] = {'title': 'Oracle Q1 Earnings Call', 'text': ''}
    assert verify_sec_event(event, parsed, {}, 'ORCL', '')['status'] == 'DOCUMENT_UNVERIFIED'


def test_matching_headline_cannot_override_conflicting_news_period():
    event, parsed = oracle()
    event['evidence'] = {'title': parsed['title'], 'text': 'Oracle reported Q2 FY27 earnings.'}
    result = verify_sec_event(event, parsed, {}, 'ORCL', '')
    assert result['status'] == 'DOCUMENT_UNVERIFIED' and result['period_conflict']
