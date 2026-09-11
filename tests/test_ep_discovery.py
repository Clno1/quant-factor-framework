from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.breakouts.ep.discovery import OfficialSourceDiscovery, exhibit_links, load_registry, select_filings
from src.breakouts.ep.dossier import candidate_dossier
from src.breakouts.ep.models import timestamp
from src.breakouts.ep.store import EpStore
from src.data.sec_attachments import SecDisclosureClient
from src.data.public_articles import HttpPage, SourceAccessError
from test_ep_sources import observed, client as public_client
from test_ep_radar import NOW, ROOT, article

CIK = "0001820953"
BASE = "https://www.sec.gov/Archives/edgar/data/1820953/000162828026059271/"
PRIMARY = BASE + "filing.htm"
EXHIBIT = BASE + "release.htm"
SUBMISSIONS = f"https://data.sec.gov/submissions/CIK{CIK}.json"
REGISTRY = {"version": "ep-issuer-registry-v1", "issuers": {"SNOW": {
    "cik": CIK, "name": "SNOW", "evidence_url": SUBMISSIONS}}}


def submissions(**changes):
    result = {"cik": 1820953, "tickers": ["SNOW"], "filings": {"recent": {
        "accessionNumber": ["0001628280-26-059271"], "filingDate": ["2026-09-02"],
        "acceptanceDateTime": ["2026-09-02T20:05:00Z"], "form": ["8-K"], "primaryDocument": ["filing.htm"]}}}
    result.update(changes)
    return result


def release(title=None, images=False):
    body = '<p><img src="a.jpg"></p>' if images else (
        f'<div>{title or article()["title"]}</div><div>' + "Revenue financial discussion. " * 20 + '</div>')
    return ('<DOCUMENT><TYPE>EX-99.1\n<SEQUENCE>2\n<FILENAME>release.htm\n<DESCRIPTION>EX-99.1\n'
            '<TEXT><html><body>' + body + '</body></html></TEXT></DOCUMENT>').encode()


class FakeClient:
    allowed_hosts = {"data.sec.gov", "www.sec.gov"}
    max_requests = 30

    def __init__(self, overrides=None):
        self.requests = 0
        self.pages = {SUBMISSIONS: json.dumps(submissions()).encode(),
                      PRIMARY: b'<table><tr><td>99.1</td><td><a href="release.htm">Quarterly results</a></td></tr></table>',
                      EXHIBIT: release()}
        self.pages.update(overrides or {})

    def validate_url(self, url):
        return SecDisclosureClient(contact_email="test@quant.test").validate_url(url)

    def _fetch(self, url, kind):
        self.validate_url(url)
        self.requests += 1
        value = self.pages.get(url)
        result = {"status": "FETCHED" if isinstance(value, bytes) else value or "SOURCE_HTTP_404",
                  "received_at": timestamp(NOW + timedelta(minutes=5)), "final_url": url, "trace": []}
        if isinstance(value, bytes):
            result[kind] = value
            result["raw_sha256"] = hashlib.sha256(value).hexdigest()
        return result

    def fetch_json(self, url):
        return self._fetch(url, "json")

    def fetch(self, url):
        return self._fetch(url, "html")

    def fetch_pdf(self, url):
        return self._fetch(url, "pdf")


def test_sec_pdf_is_archived_independently_without_automatic_financial_approval(observed, monkeypatch):
    from src.breakouts.ep.llm_contract import prepare_request
    from src.breakouts.ep.models import digest
    store, report = observed
    pdf_url = BASE + 'slides.pdf'
    primary = (b'<table><tr><td>99.1</td><td><a href="release.htm">Results</a></td></tr>'
               b'<tr><td>99.2</td><td><a href="slides.pdf">Investor presentation</a></td></tr></table>')
    parsed = {'status': 'EXTRACTED', 'page_count': 2, 'parser_version': 'fixture',
              'paragraphs': [{'id': 'page001-line0001', 'page': 1, 'text': 'Unbound financial table'}]}
    parsed['text_revision'] = digest(parsed)
    monkeypatch.setattr('src.breakouts.ep.pdf_worker.parse_pdf_bounded', lambda _: parsed)
    http = FakeClient({PRIMARY: primary, pdf_url: b'%PDF-fixture'})
    resolver = OfficialSourceDiscovery(store, http, REGISTRY, clock=lambda: NOW + timedelta(minutes=10))
    event = report['candidates'][0]['events'][0]
    result = resolver.resolve_event(report['run_id'], {'ticker': 'SNOW', 'identity': {}}, event)
    assert result['status'] == 'DOCUMENT_MATCHED'
    assert len(result['attachments']) == 1
    attachment = store.source_detail(result['attachments'][0]['source_id'])
    assert attachment['result']['status'] == 'OFFICIAL_ATTACHMENT_TEXT_AVAILABLE'
    assert attachment['result']['attachment_proof']['primary_fetch_id']
    assert attachment['result']['parent_source_id'] == result['source_id']
    assert attachment['parsed']['paragraphs'][0]['page'] == 1
    assert not attachment['result']['eligible_for_rating']
    with pytest.raises(ValueError, match='ALIGNED_ORIGINAL_TEXT'):
        prepare_request(attachment)
    assert 'PDF_EVENT_CONTEXT_NOT_VERIFIED' in result['incomplete_reasons']
    assert http.requests == 4
    second = resolver.resolve_event(report['run_id'], {'ticker': 'SNOW', 'identity': {}}, event)
    assert len(second['attachments']) == 1 and http.requests == 4
    latest = store.source_report(report['run_id'])
    assert latest['status'] == 'SOURCE_PASS_COMPLETED'
    assert len(latest['sources']) == 2
    assert {r['status'] for r in latest['sources']} == {'DOCUMENT_MATCHED', 'OFFICIAL_ATTACHMENT_TEXT_AVAILABLE'}


def test_sec_pdf_failure_and_batch_entry_are_visible(observed):
    store, report = observed
    primary = b'<table><tr><td>99.2</td><td><a href="slides.pdf">Slides</a></td></tr></table>'
    result = run(store, report, FakeClient({PRIMARY: primary, BASE + 'slides.pdf': 'SOURCE_HTTP_403'}))
    assert result['status'] == 'PARTIAL_SOURCES'
    attachments = [r for r in result['sources'] if r.get('source_route') == 'SEC_EXHIBIT_PDF']
    assert len(attachments) == 1 and attachments[0]['status'] == 'SOURCE_HTTP_403'


def test_sec_pdf_decode_count_is_bounded_per_event(observed):
    store, report = observed
    primary = ('<table>' + ''.join(f'<tr><td>99.{i}</td><td><a href="slides{i}.pdf">Slides</a></td></tr>'
                                   for i in range(1, 4)) + '</table>').encode()
    http = FakeClient({PRIMARY: primary})
    result = run(store, report, http)
    assert http.requests == 4  # submissions, primary, two failed PDF fetches
    parent = next(r for r in result['sources'] if r.get('source_route') == 'SEC_AUTO_DISCOVERY')
    assert 'PDF_ATTACHMENT_BUDGET_EXCEEDED' in parent['incomplete_reasons']


def run(store, report, http=None, **options):
    return OfficialSourceDiscovery(store, http or FakeClient(), options.pop("registry", REGISTRY),
                                   clock=lambda: NOW + timedelta(minutes=10), **options).run(report["run_id"])


def test_submissions_require_identity_aligned_columns_and_known_asof():
    assert select_filings(submissions(), "SNOW", CIK, "2026-09-02", NOW)[0]["url"] == PRIMARY
    assert select_filings(submissions(), "SNOW", CIK, "2026-09-03", NOW)[0]['url'] == PRIMARY
    assert select_filings(submissions(), "SNOW", CIK, "2026-09-04", NOW) == []
    assert select_filings(submissions(), "SNOW", CIK, "2026-09-02", NOW - timedelta(days=2)) == []
    for change in ({"cik": 1}, {"tickers": ["OTHER"]}, {"filings": {}}):
        with pytest.raises(ValueError):
            select_filings(submissions(**change), "SNOW", CIK, "2026-09-02", NOW)
    row = submissions()
    row["filings"]["recent"]["form"] = []
    with pytest.raises(ValueError):
        select_filings(row, "SNOW", CIK, "2026-09-02", NOW)


def test_sec_submissions_paths_remain_bounded():
    client = SecDisclosureClient(contact_email="test@quant.test")
    assert client.validate_url(SUBMISSIONS) == SUBMISSIONS
    for url in ("https://data.sec.gov/submissions/CIK1.json", "https://data.sec.gov/api/xbrl/companyfacts/a.json",
                "https://other.test/", "https://www.sec.gov/search", SUBMISSIONS + "?apikey=secret"):
        with pytest.raises(SourceAccessError):
            client.validate_url(url)


def test_exhibit_links_require_exhibit_label_and_same_accession():
    raw = (b'<table><tr><td>99.1</td><td><a href="release.htm#new_id-0">Results</a></td></tr>'
           b'<tr><td>99.2</td><td><a href="https://evil.test/a.htm">Other</a></td></tr>'
           b'<tr><td>99.3</td><td><a href="../other.htm">Escape</a></td></tr></table>'
           b'<a href="unlabelled.htm">Read more</a>')
    assert [row["url"] for row in exhibit_links(raw, PRIMARY)] == [EXHIBIT]


def test_automatic_discovery_archives_full_chain_and_reuses_cache(observed):
    store, report = observed
    http = FakeClient()
    first = run(store, report, http)
    assert first["status"] == "SOURCE_PASS_COMPLETED"
    assert first["sources"][0]["final_url"] == EXHIBIT
    assert first["sources"][0]["source_route"] == "SEC_AUTO_DISCOVERY"
    assert len(first["sources"][0]["discovery_steps"]) == 3
    assert len(store.fetch_history(report["run_id"])) == 3
    assert store.report(report["run_id"]) == report
    assert http.requests == 3
    second_http = FakeClient()
    second = run(store, report, second_http)
    assert second["summary"]["http_requests"] == 0
    assert second["summary"]["cached_source_urls"] == 3
    assert second_http.requests == 0
    assert second["sources"][0]["retrieved_at"] == first["sources"][0]["retrieved_at"]
    assert len(store.fetch_history(report["run_id"])) == 6
    assert all(f["cache_used"] for f in store.fetch_history(report["run_id"])[3:])
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM ep_source_fetches WHERE raw_body IS NOT NULL").fetchone()[0] == 3
    third = run(store, report, FakeClient())
    assert third["summary"]["http_requests"] == 0
    assert store.cached_fetch(SUBMISSIONS, NOW) is None
    assert store.fetch_history(report["run_id"], as_of=NOW) == []
    assert candidate_dossier(store, "SNOW", as_of=NOW)["sources"] == []


@pytest.mark.parametrize("overrides,reason", [
    ({SUBMISSIONS: "SOURCE_HTTP_429"}, "SOURCE_HTTP_429"),
    ({SUBMISSIONS: b"not-json"}, "SEC_SUBMISSIONS_UNVERIFIED"),
    ({PRIMARY: "SOURCE_TIMEOUT"}, "SOURCE_DISCOVERY_INCOMPLETE"),
    ({EXHIBIT: release(images=True)}, "SOURCE_DISCOVERY_INCOMPLETE"),
    ({EXHIBIT: release(title="Unrelated announcement")}, "NO_MATCHING_EXHIBIT_IN_FETCHED_SCOPE"),
])
def test_missing_source_does_not_become_a_negative_catalyst(observed, overrides, reason):
    store, report = observed
    result = run(store, report, FakeClient(overrides))
    assert result["sources"][0]["status"] == reason
    assert result["summary"]["ratings_enabled"] is False
    assert store.report(report["run_id"]) == report


def test_unregistered_company_is_visible_without_network(observed):
    store, report = observed
    http = FakeClient()
    result = run(store, report, http, registry={"version": "ep-issuer-registry-v1", "issuers": {}})
    assert result["sources"][0]["status"] == "ISSUER_NOT_REGISTERED"
    assert http.requests == 0


def test_dossier_keeps_earlier_body_when_latest_attempt_fails(observed):
    store, report = observed
    run(store, report)
    failed = store.start_source_run(report["run_id"], {}, NOW + timedelta(minutes=11))
    event = report["candidates"][0]["events"][0]
    store.save_source_attempt(failed, "SNOW", event, {"status": "SOURCE_TIMEOUT"}, NOW + timedelta(minutes=11))
    store.finish_source_run(failed, {"status": "PARTIAL_SOURCES"}, NOW + timedelta(minutes=11))
    dossier = candidate_dossier(store, "SNOW")
    assert dossier["latest_source_attempts"][0]["status"] == "SOURCE_TIMEOUT"
    assert dossier["sources"][0]["url"] == EXHIBIT
    assert dossier["facts"] == []
    assert dossier["grade"] is None
    assert dossier["eligible_for_rating"] is False
    assert candidate_dossier(store, "MISSING")["status"] == "NOT_FOUND_IN_FETCHED_SCOPE"


def test_registry_limits_and_schema_three_readonly_upgrade(observed, tmp_path):
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(REGISTRY))
    assert load_registry(path) == REGISTRY
    bad = deepcopy(REGISTRY)
    bad["issuers"]["SNOW"]["cik"] = "../secret"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        load_registry(path)
    store, report = observed
    with store.connection() as db:
        db.execute("DROP TABLE ep_source_fetches")
        db.execute("UPDATE ep_schema SET version=3")
    old = EpStore(store.path, read_only=True)
    assert old.schema_version == 3
    assert old.fetch_history(report["run_id"]) == []
    assert EpStore(store.path).schema_version == 6
    assert old.report(report["run_id"]) == report


def test_refusal_cache_does_not_repeat_network_and_does_not_mark_matched(observed):
    store, report = observed
    run(store, report, FakeClient({SUBMISSIONS: "SOURCE_HTTP_429"}))
    http = FakeClient()
    result = run(store, report, http)
    assert http.requests == 0
    assert result["sources"][0]["status"] == "SOURCE_HTTP_429"


def test_sec_exhibit_label_is_not_used_as_article_title():
    from src.breakouts.ep.sec_source import parse_sec_attachment
    raw = release().replace(b"<body>", b"<body><div>Exhibit 99.1</div>")
    assert parse_sec_attachment(raw, EXHIBIT)["title"] == article()["title"]


def test_ambiguous_same_title_exhibits_require_review(observed):
    store, report = observed
    primary = b'<table><tr><td>99.1</td><td><a href="release.htm">Results</a></td></tr>'
    primary += b'<tr><td>99.2</td><td><a href="other.htm">Results corrected</a></td></tr></table>'
    http = FakeClient({PRIMARY: primary, BASE + "other.htm": release().replace(b"release.htm", b"other.htm")})
    result = run(store, report, http)
    assert result["sources"][0]["status"] == "AMBIGUOUS_MATCH_REVIEW_REQUIRED"
    assert candidate_dossier(store, "SNOW")["sources"] == []


@pytest.mark.parametrize("url,reason", [(PRIMARY, "PRIMARY_FILING_PARSE_ERROR"), (EXHIBIT, "UNSUPPORTED_SEC_ATTACHMENT")])
def test_malformed_remote_html_is_recorded_without_aborting_batch(observed, url, reason):
    store, report = observed
    result = run(store, report, FakeClient({url: b""}))
    assert result["status"] == "PARTIAL_SOURCES"
    assert reason in result["sources"][0]["incomplete_reasons"]


def test_exhibit_budget_preserves_visible_coverage_gap(observed):
    store, report = observed
    primary = b'<table><tr><td>99.1</td><td><a href="release.htm">Results</a></td></tr>'
    primary += b'<tr><td>99.2</td><td><a href="other.htm">Other</a></td></tr></table>'
    result = run(store, report, FakeClient({PRIMARY: primary}), max_exhibits=1)
    assert result["sources"][0]["status"] == "DOCUMENT_MATCHED"
    assert result["sources"][0]["search_complete"] is False
    assert "EXHIBIT_BUDGET_EXCEEDED" in result["sources"][0]["incomplete_reasons"]


def test_corrupt_cached_bytes_are_not_reused(observed):
    store, report = observed
    run(store, report)
    with store.connection() as db:
        db.execute("UPDATE ep_source_fetches SET raw_body=? WHERE url=?", (b"tampered", SUBMISSIONS))
    assert store.cached_fetch(SUBMISSIONS, NOW + timedelta(minutes=11)) is None


def test_fetch_receipt_cannot_backfill_future_data(observed):
    store, report = observed
    batch = store.start_source_run(report["run_id"], {}, NOW)
    with pytest.raises(ValueError):
        store.save_fetch(batch, SUBMISSIONS, {"received_at": timestamp(NOW + timedelta(days=1))}, NOW, b"{}")


def test_json_fetch_is_explicit_and_does_not_accept_html_challenges():
    url = "https://example.test/submissions"
    http = public_client({"/submissions": HttpPage(200, {"content-type": "application/json"}, b'{"cik": 1}')})
    assert http.fetch_json(url)["json"] == b'{"cik": 1}'
    assert http.fetch(url)["status"] == "NON_HTML_RESPONSE"
    assert public_client().fetch_json(url)["status"] == "NON_JSON_RESPONSE"


def test_readonly_cli_needs_no_fmp_or_parser_initialization(observed):
    store, report = observed
    run(store, report)
    for args in (["dossier", "SNOW"], ["fetches"]):
        process = subprocess.run([sys.executable, "-S", str(ROOT / "scripts/run_ep_radar.py"),
            "--db", str(store.path), *args], capture_output=True, text=True)
        assert process.returncode == 0, process.stderr
        assert json.loads(process.stdout)
