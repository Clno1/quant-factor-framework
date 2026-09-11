from copy import deepcopy
from datetime import timedelta
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from src.breakouts.ep.official_sources import OfficialSourceRouter, load_official_registry, period_key, trusted_ir_source, dynamic_news_endpoints
from src.breakouts.ep.llm_contract import prepare_request
from src.breakouts.ep.pdf_worker import parse_pdf_bounded
from src.breakouts.ep.models import digest
from src.data.public_articles import HttpPage
from test_ep_sources import observed, client, html, TITLE
from test_ep_radar import NOW, ROOT

REGISTRY = {'version': 'ep-official-domains-v1', 'issuers': {'SNOW': {
    'name': 'SNOW', 'cik': '0001820953', 'root_url': 'https://example.test/corp',
    'ir_host': 'example.test', 'index_urls': ['https://example.test/news']}}}


def router(store, *, root=True, pdf=False, monkeypatch=None):
    release = html(text='SNOW (NASDAQ: SNOW) today reported results. Revenue was $928 million. The company describes operating performance and its business outlook. ')
    if pdf:
        release += b'<a href="https://example.test/letter.pdf">Shareholder Letter</a>'
        def parsed(_):
            body = {'status': 'EXTRACTED', 'title': TITLE, 'parser_version': 'fixture',
                    'paragraphs': [{'id': 'p0001', 'text': 'SNOW second quarter fiscal 2027 shareholder letter.'}]}
            body['text_revision'] = digest(body)
            return body
        monkeypatch.setattr('src.breakouts.ep.pdf_worker.parse_pdf_bounded', parsed)
    pages = {'/corp': HttpPage(200, {'content-type': 'text/html'}, b'<a href="/news">Investors</a>' if root else b'<p>No IR link</p>'),
             '/news': HttpPage(200, {'content-type': 'text/html'}, ('<a href="/release">' + TITLE + '</a>').encode()),
             '/release': HttpPage(200, {'content-type': 'text/html'}, release),
             '/letter.pdf': HttpPage(200, {'content-type': 'application/pdf'}, b'%PDF-1.7\nfixture')}
    http = client(pages)
    original_fetch = http.fetch_same_host
    def fetch(url, **kwargs):
        result = original_fetch(url, **kwargs)
        result['received_at'] = (NOW + timedelta(minutes=5)).isoformat()
        return result
    http.fetch_same_host = fetch
    resolver = OfficialSourceRouter(store, None, {'version': 'v1', 'issuers': {}}, http, REGISTRY,
                                    clock=lambda: NOW + timedelta(minutes=10))
    resolver._resolve = resolver._resolve_ir
    return resolver, http


def resolve(observed, **kwargs):
    store, report = observed
    resolver, http = router(store, **kwargs)
    candidate = {'ticker': 'SNOW', 'identity': {'ticker': 'SNOW', 'name': 'SNOW', 'cik': '0001820953'}}
    event = report['candidates'][0]['events'][0]
    result = resolver.resolve_event(report['run_id'], candidate, event)
    return result, store, http


def test_verified_ir_to_llm_and_receipts(observed):
    result, store, http = resolve(observed)
    assert result['status'] == 'DOCUMENT_MATCHED', result
    source = store.source_detail(result['source_id'])
    assert trusted_ir_source(source)
    assert prepare_request(source)['untrusted_paragraphs']
    assert result['issuer_proof']['root_fetch_id'] and result['fetch_id']
    assert len(http.test_calls) <= 6
    source['result']['issuer_proof']['cik'] = '0000000001'
    assert not trusted_ir_source(source)
    with pytest.raises(ValueError, match='SEC_ORIGINAL_SOURCE'):
        prepare_request(source)


def test_root_link_is_required(observed):
    result, _, _ = resolve(observed, root=False)
    assert result['status'] == 'OFFICIAL_ROOT_IR_LINK_MISSING'


@pytest.mark.parametrize('status', ['SOURCE_TIMEOUT', 'SOURCE_HTTP_403', 'ROBOTS_DISALLOWED'])
def test_source_access_failure_is_not_reported_as_no_catalyst(observed, status):
    store, report = observed
    resolver, http = router(store)
    fetch = http.fetch_same_host
    def blocked(url, **kwargs):
        result = fetch(url, **kwargs)
        if url.endswith('/news'):
            result.pop('html', None)
            result['status'] = status
        return result
    http.fetch_same_host = blocked
    event = deepcopy(report['candidates'][0]['events'][0])
    event['evidence']['url'] = 'https://wire.test/news'
    result = resolver.resolve_event(report['run_id'],
        {'ticker': 'SNOW', 'identity': {'cik': '0001820953'}}, event)
    assert result['status'] == 'IR_SOURCE_ACCESS_INCOMPLETE'
    assert result['blocking_reasons'] == [status]


def test_sg_dynamic_index_reports_unimplemented_feed_without_running_scripts(observed):
    store, report = observed
    resolver, http = router(store)
    config = {'rest': {'external': {'releases': {'latestReleases': 'https://example.test/wp-json/anfco/v1/latest-releases'}}}}
    raw = ('<script>var anfcoUrls = ' + json.dumps(config) + '; throw Error("must not run");</script>').encode()
    assert dynamic_news_endpoints(raw, 'https://example.test/news') == [config['rest']['external']['releases']['latestReleases']]
    assert dynamic_news_endpoints(raw, 'https://other.test/news') == []
    fetch = http.fetch_same_host
    def dynamic(url, **kwargs):
        result = fetch(url, **kwargs)
        if url.endswith('/news'):
            result['html'] = raw
        return result
    http.fetch_same_host = dynamic
    event = deepcopy(report['candidates'][0]['events'][0])
    event['evidence']['url'] = 'https://wire.test/news'
    result = resolver.resolve_event(report['run_id'], {'ticker': 'SNOW', 'identity': {'cik': '0001820953'}}, event)
    assert result['status'] == 'IR_SOURCE_ACCESS_INCOMPLETE'
    assert 'IR_DYNAMIC_FEED_NOT_INTEGRATED' in result['blocking_reasons']
    assert not any('wp-json' in url for url, *_ in http.test_calls)


def test_negative_ir_cache_avoids_new_requests_in_next_cycle(observed):
    store, report = observed
    resolver, http = router(store)
    fetch = http.fetch_same_host
    def refused(url, **kwargs):
        result = fetch(url, **kwargs)
        if url.endswith('/news'):
            result.pop('html', None)
            result['status'] = 'SOURCE_HTTP_403'
        return result
    http.fetch_same_host = refused
    event = deepcopy(report['candidates'][0]['events'][0])
    event['evidence']['url'] = 'https://wire.test/news'
    candidate = {'ticker': 'SNOW', 'identity': {'cik': '0001820953'}}
    resolver.resolve_event(report['run_id'], candidate, event)
    requests = http.requests
    resolver.resolve_event(report['run_id'], candidate, event)
    assert http.requests == requests
    assert http.blocked['example.test'] == 'SOURCE_HTTP_403'


def test_current_cik_mismatch_blocks_before_network(observed):
    store, report = observed
    resolver, http = router(store)
    result = resolver.resolve_event(report['run_id'], {'ticker': 'SNOW', 'identity': {'cik': '1'}},
                                    report['candidates'][0]['events'][0])
    assert result['status'] == 'IR_CURRENT_CIK_NOT_VERIFIED' and http.requests == 0


def test_cross_issuer_redirect_rejected_before_connection():
    http = client({'/a': HttpPage(302, {'location': 'https://other.test/b'}, b'')},
                  allowed_hosts={'example.test', 'other.test'})
    assert http.fetch_same_host('https://example.test/a')['status'] == 'ISSUER_REDIRECT_OUTSIDE_HOST'
    assert all('other.test' not in call[0] for call in http.test_calls)


def test_pdf_is_separate_issuer_period_bound_receipt(observed, monkeypatch):
    result, store, _ = resolve(observed, pdf=True, monkeypatch=monkeypatch)
    assert result['attachments'][0]['status'] == 'DOCUMENT_MATCHED'
    attachment = store.source_detail(result['attachments'][0]['source_id'])
    assert attachment['result']['parent_release_fetch_id'] == result['fetch_id']
    assert trusted_ir_source(attachment)
    assert prepare_request(attachment)['untrusted_paragraphs'][0]['text'].startswith('SNOW')
    assert len(store.aligned_sources(attachment['run_id'], 'SNOW')) == 2
    assert len(store.source_report(attachment['run_id'])['sources']) == 2


def test_release_and_pdf_become_distinct_durable_analysis_jobs(observed, monkeypatch, tmp_path):
    from src.breakouts.ep.queue import PipelineQueue
    from src.breakouts.ep.pipeline import process_sources
    store, report = observed
    now = NOW + timedelta(minutes=10)
    store.save_profile(report['run_id'], 'SNOW', now, 'OK', {'ticker': 'SNOW', 'name': 'SNOW',
        'cik': '0001820953', 'exchange': 'NASDAQ', 'asset_type': 'STOCK', 'is_actively_trading': True})
    event = report['candidates'][0]['events'][0]
    queue = PipelineQueue(tmp_path / 'pipeline.sqlite3')
    queue.enqueue('SOURCE', 'SNOW', event['document_id'], event['revision_id'],
                  {'run_id': report['run_id'], 'event': event}, now, now + timedelta(hours=12))
    resolver, _ = router(store, pdf=True, monkeypatch=monkeypatch)
    result = process_sources(queue, store, resolver, SimpleNamespace(source_jobs_per_cycle=1), clock=lambda: now)
    assert result == {'DOCUMENT_MATCHED': 1}
    ready = queue.ready('ANALYSIS', now)
    assert len(ready) == 2 and ready[0]['document_id'] != ready[1]['document_id']
    for job in ready:
        assert prepare_request(store.source_detail(job['payload']['source_id'], as_of=now))


def test_pdf_subprocess_and_registry(tmp_path):
    import pypdf
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buf = BytesIO()
    writer.write(buf)
    assert parse_pdf_bounded(buf.getvalue())['status'] == 'BODY_NOT_ESTABLISHED'
    assert parse_pdf_bounded(b'notpdf')['status'] == 'PDF_BYTES_INVALID'
    assert load_official_registry(ROOT / 'configs/ep_official_domains.json')['issuers']['AFRM']['cik']
    bad = deepcopy(REGISTRY)
    bad['issuers']['SNOW']['root_url'] = 'https://user:pass@example.test/'
    path = tmp_path / 'registry.json'
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match='ROOT_URL'):
        load_official_registry(path)
    assert period_key('fiscal 2026 fourth quarter') == {('4', '2026')}
