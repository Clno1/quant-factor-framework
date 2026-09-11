import pytest

from src.breakouts.ep.event_ingest import registry_for_candidates
from src.breakouts.ep.discovery import OfficialSourceDiscovery
from src.data.sec_company_index import SecCompanyIndexClient, INDEX_URL
from src.data.public_articles import SourceAccessError
from test_ep_discovery import run, FakeClient, REGISTRY
from test_ep_sources import observed
from test_ep_radar import FakeProvider, NOW, article, financials
from src.breakouts.ep.models import EpSettings
from src.breakouts.ep.service import EpRadar
from src.breakouts.ep.store import EpStore


def test_registry_uses_official_index_and_does_not_guess_ambiguous_tickers():
    payload = {"0": {"ticker": "AEVA", "cik_str": 123, "title": "Aeva"},
               "1": {"ticker": "TEST", "cik_str": 456, "title": "Company"},
               "2": {"ticker": "TEST", "cik_str": 789, "title": "Other company"}}
    registry, missing = registry_for_candidates(payload, ["AEVA", "TEST", "NEW"])
    assert registry["issuers"]["AEVA"]["cik"] == "0000000123"
    assert registry["issuers"]["AEVA"]["evidence_url"] == INDEX_URL
    assert missing == ["NEW", "TEST"]
    payload["0"]["cik_str"] = True
    with pytest.raises(ValueError):
        registry_for_candidates(payload, ["AEVA"])


def test_company_index_endpoint_does_not_expand_arbitrary_sec_access():
    client = SecCompanyIndexClient(contact_email="research@valid-company.com")
    assert client.validate_url(INDEX_URL) == INDEX_URL
    for url in (INDEX_URL + "?token=foo", "https://www.sec.gov/files/other.json", "http://www.sec.gov/files/company_tickers.json"):
        with pytest.raises(SourceAccessError):
            client.validate_url(url)


def test_skip_existing_verified_source_preserves_original_evidence(observed):
    store, report = observed
    first = run(store, report)
    before = store.aligned_sources(report["run_id"], "SNOW")
    client = FakeClient()
    discovery = OfficialSourceDiscovery(store, client, REGISTRY, skip_existing=True)
    discovery.run(report["run_id"])
    assert store.aligned_sources(report["run_id"], "SNOW") == before
    assert client.requests == 0


def test_online_profiles_prioritize_registered_recent_events_before_alphabetical(tmp_path):
    provider = FakeProvider(articles=[article('FOREIGN'), article('AAA'),
                            article('ZZZ', publishedDate='2026-09-03 08:00:00')], calendar=[financials('CALENDAR')])
    EpRadar(EpStore(tmp_path / 'ep.sqlite3'), provider, EpSettings(max_profiles=1),
            clock=lambda: NOW, priority_symbols={'AAA', 'ZZZ'}).collect('2026-09-02', '2026-09-03')
    assert [c[1] for c in provider.calls if c[0] == 'profile'] == ['ZZZ']


def test_current_source_queue_accepts_fresh_known_identity(observed):
    store, report = observed
    result = run(store, report, prioritize_current=True)
    assert result['summary']['counts']['DOCUMENT_MATCHED'] == 1


def test_current_source_queue_does_not_spend_on_stale_events(observed):
    from datetime import timedelta
    store, report = observed
    client = FakeClient()
    result = OfficialSourceDiscovery(store, client, REGISTRY, prioritize_current=True,
                                      clock=lambda: NOW + timedelta(days=1)).run(report['run_id'])
    assert client.requests == 0
    assert result['summary']['counts']['OUTSIDE_CURRENT_PROVIDER_WINDOW'] == 1


def test_lsak_released_dateline_keeps_issuer_and_announcement_date_bound():
    from src.breakouts.ep.catalyst import release_date
    source = {'ticker': 'LSAK', 'parsed': {'paragraphs': [{'id': 'p0003', 'text':
        'JOHANNESBURG, September 9, 2026 - Lesaka Technologies, Inc. (Nasdaq: LSAK; JSE: LSK) '
        'today released results for the fourth quarter and full year of fiscal 2026.'}]}}
    assert release_date(source)['date'] == '2026-09-09'
    source['ticker'] = 'OTHER'
    assert release_date(source) is None
