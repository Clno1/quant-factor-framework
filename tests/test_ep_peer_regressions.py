import base64
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from src.breakouts.ep.discovery import OfficialSourceDiscovery, select_filings
from src.breakouts.ep.models import CatalystSnapshot, timestamp
from src.breakouts.ep.classifier import classify
from src.breakouts.ep.store import EpStore
from src.breakouts.ep.source_verifier import verify_sec_event
from src.breakouts.ep.sec_source import parse_sec_attachment

BUNDLE = Path(__file__).parent / 'fixtures/ep_peer_sources_20260911.json'


def cases():
    return json.loads(BUNDLE.read_text())['cases']


def decode(fetch):
    raw = gzip.decompress(base64.b64decode(fetch['gzip_base64']))
    if raw:
        assert hashlib.sha256(raw).hexdigest() == fetch['result']['raw_sha256']
    return raw


class ArchivedClient:
    max_requests = 30
    requests = 0

    def __init__(self, case, now):
        self.pages = {f['url']: f for f in case['fetches']}
        self.now = now

    def validate_url(self, url):
        assert url in self.pages, 'Replay may not fetch an unarchived URL: ' + url

    def fetch(self, url, kind='html'):
        self.validate_url(url)
        self.requests += 1
        fetch = self.pages[url]
        return {**fetch['result'], kind: decode(fetch), 'received_at': timestamp(self.now)}

    def fetch_json(self, url):
        return self.fetch(url, 'json')


def test_orcl_archived_submissions_include_prior_evening_not_future():
    case = cases()[0]
    payload = json.loads(decode(case['fetches'][0]))
    now = datetime(2026, 9, 11, 12, 15, tzinfo=timezone.utc)
    cik = case['result']['registry_entry']['cik']
    found = select_filings(payload, 'ORCL', cik, '2026-09-11', now)
    assert '0001193125-26-387905' in {r['accessionNumber'] for r in found}
    assert all(datetime.fromisoformat(r['acceptanceDateTime'].replace('Z', '+00:00')) <= now for r in found)
    assert select_filings(payload, 'ORCL', cik, '2026-09-11', now - timedelta(days=2)) == []


@pytest.mark.parametrize('symbol', ['CPRT', 'RH'])
def test_archived_source_chain_resolves_primary_and_keeps_support(tmp_path, symbol):
    case = next(c for c in cases() if c['ticker'] == symbol)
    now = datetime.fromisoformat(case['observed_at']) + timedelta(minutes=1)
    store = EpStore(tmp_path / 'sources.sqlite3')
    doc = case['document']
    payload = json.loads(doc['payload_json'])
    event = classify({**doc, 'payload': payload})
    run = store.start_run('2026-09-10', '2026-09-11', {}, now)
    snapshot = CatalystSnapshot(doc['document_id'], doc['revision_id'], symbol, 'ARTICLE',
        doc['event_date'], doc['published_at'], doc['first_seen_at'], payload)
    store.save_page(run, {}, [snapshot])
    entry = case['result']['registry_entry']
    resolver = OfficialSourceDiscovery(store, ArchivedClient(case, now),
        {'version': 'ep-issuer-registry-v1', 'issuers': {symbol: entry}}, clock=lambda: now)
    result = resolver.resolve_event(run, {'ticker': symbol, 'identity': {'name': entry['name']}}, event)
    assert result['status'] == 'DOCUMENT_MATCHED', result
    assert len(result['supporting_documents']) == 1
    assert len(result['attachments']) == 1
    assert result['attachments'][0]['status'] == 'OFFICIAL_SUPPORTING_TEXT_AVAILABLE'
    source = store.source_detail(result['source_id'])
    assert '\u200b' not in source['parsed']['title']
    if symbol == 'RH':
        assert source['parsed']['title'] == 'RH REPORTS SECOND QUARTER FISCAL 2026 RESULTS'
        wrong = {**event, 'evidence': {**event['evidence'], 'title': 'RH Q1 Earnings',
                                     'text': 'First quarter fiscal 2026 results'}}
        assert verify_sec_event(wrong, source['parsed'], {}, symbol, result['exhibit_label'])['status'] == 'DOCUMENT_UNVERIFIED'


def test_real_rh_body_does_not_create_earnings_match_for_acquisition():
    case = cases()[2]
    fetch = next(f for f in case['fetches'] if f['url'].endswith('ex99d1.htm'))
    parsed = parse_sec_attachment(decode(fetch), fetch['url'])
    event = {'evidence': {'title': 'RH acquires another company', 'text': 'Second quarter fiscal 2026'}}
    assert verify_sec_event(event, parsed, {}, 'RH', '')['status'] == 'DOCUMENT_UNVERIFIED'
