"""Issuer-scoped IR discovery with archived link proofs and SEC fallback."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from .discovery import OfficialSourceDiscovery
from .models import digest, ticker, timestamp
from .source_verifier import parse_article, verify_document, _name

LINKAGE = 'CURATED_CIK_AND_OFFICIAL_DOMAIN_LINK_MATCH'


def load_official_registry(path):
    path = Path(path)
    if path.stat().st_size > 200_000:
        raise ValueError('OFFICIAL_REGISTRY_TOO_LARGE')
    value = json.loads(path.read_text())
    if value.get('version') != 'ep-official-domains-v1' or not isinstance(value.get('issuers'), dict):
        raise ValueError('OFFICIAL_REGISTRY_INVALID')
    if len(value['issuers']) > 500:
        raise ValueError('OFFICIAL_REGISTRY_TOO_LARGE')
    for symbol, row in value['issuers'].items():
        if not isinstance(row, dict) or ticker(symbol) != symbol or not re.fullmatch(r'[0-9]{10}', row.get('cik', '')) or not row.get('name'):
            raise ValueError('OFFICIAL_ISSUER_IDENTITY_REQUIRED')
        root = urlsplit(row['root_url'])
        if root.scheme != 'https' or not root.hostname or root.port or root.username or root.password or root.query or root.fragment:
            raise ValueError('OFFICIAL_ROOT_URL_INVALID')
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', row['ir_host']):
            raise ValueError('EXACT_IR_HOST_REQUIRED')
        if not 1 <= len(row['index_urls']) <= 3:
            raise ValueError('BOUNDED_IR_INDEX_REQUIRED')
        for url in row['index_urls']:
            if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != row['ir_host']:
                raise ValueError('IR_INDEX_OUTSIDE_ISSUER_HOST')
    return value


def links(raw, base):
    from lxml import html
    root = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
    result = []
    for node in root.xpath('//a[@href]')[:5000]:
        url = urljoin(base, node.get('href')).split('#')[0]
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.username or parts.password or parts.query:
            continue
        label = ' '.join(' '.join(node.itertext()).split())[:500]
        result.append({'url': url, 'label': label})
    return result


def period_key(text):
    words = {'first': 1, 'second': 2, 'third': 3, 'fourth': 4}
    matches = re.findall(r'\bQ([1-4])\s*(?:FY\s*)?(20\d{2})\b', text, re.I)
    matches += [(str(words[q.lower()]), y) for q, y in re.findall(
        r'\b(first|second|third|fourth)\s+(?:fiscal\s+)?quarter\s+(?:of\s+)?(?:fiscal\s+)?(?:year\s+)?(20\d{2})', text, re.I)]
    matches += [(str(words[q.lower()]), y) for y, q in re.findall(
        r'\bfiscal\s+(20\d{2})\s+(first|second|third|fourth)\s+quarter', text, re.I)]
    return set(matches)


def dynamic_news_endpoints(raw, base):
    """Inspect a known public configuration object, without executing JavaScript."""
    from lxml import html
    root = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
    found = []
    for text in root.xpath('//script[not(@src)]/text()'):
        match = re.search(r'\bvar\s+anfcoUrls\s*=\s*(?=\{)', text)
        if not match:
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end():])
            url = value['rest']['external']['releases']['latestReleases']
            parts = urlsplit(url)
            if (parts.scheme == 'https' and parts.netloc == urlsplit(base).netloc
                    and not parts.query and not parts.fragment and not parts.username and not parts.password):
                found.append(url)
        except (ValueError, TypeError, KeyError):
            continue
    return sorted(set(found))[:2]


class OfficialSourceRouter(OfficialSourceDiscovery):
    def __init__(self, store, sec_client, registry, public_client, official_registry, **kwargs):
        merged = {'version': registry['version'], 'issuers': dict(registry['issuers'])}
        for symbol, row in official_registry['issuers'].items():
            existing = merged['issuers'].get(symbol)
            if existing and existing['cik'] != row['cik']:
                continue
            merged['issuers'].setdefault(symbol, {'cik': row['cik'], 'name': row['name']})
        super().__init__(store, sec_client, merged, **kwargs)
        self.public, self.official = public_client, official_registry

    def _public_fetch(self, batch, url, kind='html', ttl=900):
        self.public.validate_url(url)
        cached = self.store.cached_fetch(url, self.clock())
        reusable = {'FETCHED', 'SOURCE_TIMEOUT', 'SOURCE_HTTP_401', 'SOURCE_HTTP_403',
                    'SOURCE_HTTP_429', 'ROBOTS_DISALLOWED'}
        if cached and cached['result'].get('kind') == kind and cached['result']['status'] in reusable:
            age = self.clock() - datetime.fromisoformat(cached['result']['received_at'])
            if cached['result']['status'] != 'FETCHED':
                ttl = min(ttl, 900)
            if timedelta(0) <= age < timedelta(seconds=ttl):
                status = cached['result']['status']
                if status in {'SOURCE_TIMEOUT', 'SOURCE_HTTP_401', 'SOURCE_HTTP_403', 'SOURCE_HTTP_429'}:
                    self.public.blocked[urlsplit(url).hostname] = status
                result = {**cached['result'], 'cache_used': True, 'body_fetch_id': cached['body_fetch_id']}
                fid = self.store.save_fetch(batch, url, result, self.clock())
                return result, cached['raw'], fid
        result = self.public.fetch_same_host(url, kind=kind)
        raw = result.pop(kind, None)
        result.update(kind=kind, cache_used=False)
        fid = self.store.save_fetch(batch, url, result, self.clock(), raw=raw)
        return result, raw, fid

    def _resolve_ir(self, batch, candidate, event):
        symbol, identity = candidate['ticker'], candidate['identity']
        row = self.official['issuers'].get(symbol)
        trace = []
        base = {'source_route': 'VERIFIED_IR_DISCOVERY', 'discovery_steps': trace,
                'coverage': 'CURATED_ISSUER_INDEX_AND_LINKS_ONLY'}
        if row is None:
            return {**base, 'status': 'IR_ISSUER_NOT_REGISTERED'}, None, None
        cik = str(identity.get('cik') or '').zfill(10)
        if cik != row['cik'] or self.registry['issuers'][symbol]['cik'] != cik:
            return {**base, 'status': 'IR_CURRENT_CIK_NOT_VERIFIED'}, None, None
        response, raw, fid = self._public_fetch(batch, row['root_url'], ttl=30 * 86400)
        trace.append({'stage': 'OFFICIAL_ROOT', 'url': row['root_url'], 'fetch_id': fid, 'status': response['status']})
        if response['status'] != 'FETCHED':
            return {**base, 'status': response['status']}, None, None
        if urlsplit(response['final_url']).hostname != urlsplit(row['root_url']).hostname:
            return {**base, 'status': 'OFFICIAL_ROOT_REDIRECT_REVIEW_REQUIRED'}, None, None
        root_links = links(raw, response['final_url'])
        if not any(urlsplit(item['url']).netloc == row['ir_host'] for item in root_links):
            return {**base, 'status': 'OFFICIAL_ROOT_IR_LINK_MISSING'}, None, None
        proof = {'ticker': symbol, 'cik': cik, 'host': row['ir_host'], 'root_url': row['root_url'],
                 'root_fetch_id': fid, 'root_sha256': response['raw_sha256'],
                 'registry_revision': digest(self.official), 'identity_observed_at': timestamp(self.clock())}
        base.update(issuer_linkage=LINKAGE, issuer_proof=proof, registry_entry=row)
        targets = []
        index_attachments = []
        original = event['evidence']['url']
        if urlsplit(original).netloc == row['ir_host']:
            targets.append(original)
        title_words = set(re.findall(r'[a-z0-9]+', event['evidence']['title'].lower()))
        for index in row['index_urls']:
            response, raw, fid = self._public_fetch(batch, index)
            trace.append({'stage': 'IR_INDEX', 'url': index, 'fetch_id': fid, 'status': response['status']})
            if response['status'] != 'FETCHED' or urlsplit(response['final_url']).netloc != row['ir_host']:
                continue
            for endpoint in dynamic_news_endpoints(raw, index):
                trace.append({'stage': 'IR_DYNAMIC_INDEX', 'url': endpoint,
                              'parent_index_fetch_id': fid, 'status': 'IR_DYNAMIC_FEED_NOT_INTEGRATED'})
            for link in links(raw, index):
                if (urlsplit(link['url']).netloc == row['ir_host']
                        and 'shareholder letter' in link['label'].lower()
                        and period_key(link['label']) & period_key(event['evidence']['title'])):
                    index_attachments.append({**link, 'parent_index_fetch_id': fid})
                words = set(re.findall(r'[a-z0-9]+', link['label'].lower()))
                if (urlsplit(link['url']).netloc == row['ir_host'] and title_words
                        and len(title_words & words) / len(title_words) >= .70):
                    targets.append(link['url'])
        for url in list(dict.fromkeys(targets))[:self.max_exhibits]:
            response, raw, fid = self._public_fetch(batch, url, ttl=86400)
            trace.append({'stage': 'IR_RELEASE', 'url': url, 'fetch_id': fid, 'status': response['status']})
            if response['status'] != 'FETCHED' or urlsplit(response['final_url']).netloc != row['ir_host']:
                continue
            parsed = parse_article(raw)
            verification = verify_document(event, parsed, identity)
            if verification['status'] != 'DOCUMENT_MATCHED':
                trace[-1]['verification'] = verification
                continue
            intro = ' '.join(p['text'] for p in parsed['paragraphs'][:12])
            if not re.search(r'\b' + re.escape(symbol) + r'\b', intro, re.I) or _name(row['name']) not in _name(intro):
                trace[-1]['status'] = 'IR_BODY_ISSUER_NOT_ESTABLISHED'
                continue
            verification['issuer_status'] = 'ISSUER_ATTRIBUTION_MATCH'
            result = {**base, 'status': 'DOCUMENT_MATCHED', 'verification': verification,
                      'final_url': response['final_url'], 'retrieved_at': response['received_at'],
                      'raw_sha256': response['raw_sha256'], 'fetch_id': fid,
                      'text_revision': parsed['text_revision'], 'parser_version': parsed['parser_version']}
            # Attachments are separate receipts. They do not overwrite the release or inherit its numbers.
            attachments = []
            for link in links(raw, url) + index_attachments:
                if len(attachments) >= 2:
                    break
                if urlsplit(link['url']).netloc != row['ir_host'] or not re.search(r'\.pdf$|shareholder letter', link['url'] + ' ' + link['label'], re.I):
                    continue
                from .pdf_worker import parse_pdf_bounded
                pdf_result, body, pid = self._public_fetch(batch, link['url'], kind='pdf', ttl=86400)
                meta = {**base, 'status': pdf_result['status'], 'parent_release_fetch_id': fid,
                        'final_url': pdf_result.get('final_url'), 'fetch_id': pid,
                        'retrieved_at': pdf_result['received_at'], 'relationship': 'OFFICIAL_RELEASE_ATTACHMENT'}
                if link.get('parent_index_fetch_id'):
                    meta.update(relationship='OFFICIAL_QUARTERLY_INDEX_ATTACHMENT',
                                parent_index_fetch_id=link['parent_index_fetch_id'])
                pdf = None
                if pdf_result['status'] == 'FETCHED':
                    pdf = parse_pdf_bounded(body)
                    text = ' '.join(p['text'] for p in pdf.get('paragraphs', []))
                    match = (pdf['status'] == 'EXTRACTED' and _name(row['name']) in _name(text)
                             and bool(period_key(event['evidence']['title']) & period_key(text)))
                    meta.update(status='DOCUMENT_MATCHED' if match else 'PDF_CONTEXT_NOT_ESTABLISHED',
                                verification={'status': 'DOCUMENT_MATCHED' if match else 'DOCUMENT_UNVERIFIED',
                                              'issuer_status': 'ISSUER_ATTRIBUTION_MATCH' if match else 'ISSUER_UNVERIFIED',
                                              'method': 'OFFICIAL_RELEASE_LINK_AND_PDF_ISSUER_PERIOD'},
                                text_revision=pdf.get('text_revision'), raw_sha256=pdf_result['raw_sha256'])
                self.pending_attachments.append((meta, body, pdf, link['url']))
                attachments.append({'status': meta['status'], 'url': link['url']})
            result['attachments'] = attachments
            return result, raw, parsed
        failures = sorted({step['status'] for step in trace if step.get('status') not in {'FETCHED', 'IR_BODY_ISSUER_NOT_ESTABLISHED'}
                           and step.get('stage') in {'IR_INDEX', 'IR_RELEASE', 'IR_DYNAMIC_INDEX'}})
        return {**base, 'status': ('IR_SOURCE_ACCESS_INCOMPLETE' if failures
                                  else 'NO_MATCHING_IR_RELEASE_IN_FETCHED_SCOPE'),
                'blocking_reasons': failures}, None, None

    def _resolve(self, batch, candidate, event):
        from src.data.public_articles import SourceAccessError
        try:
            result, raw, parsed = self._resolve_ir(batch, candidate, event)
        except (ValueError, TypeError, KeyError, SourceAccessError) as exc:
            result, raw, parsed = {'status': 'IR_SOURCE_' + type(exc).__name__}, None, None
        if result['status'] == 'DOCUMENT_MATCHED' or self.client is None:
            return result, raw, parsed
        fallback, raw, parsed = super()._resolve(batch, candidate, event)
        fallback['ir_attempt'] = result
        return fallback, raw, parsed


def trusted_ir_source(source):
    result = source['result']
    proof = result.get('issuer_proof') or {}
    url = urlsplit(result.get('final_url', ''))
    return (result.get('issuer_linkage') == LINKAGE and url.scheme == 'https' and not url.query
            and not url.username and not url.password and url.netloc == proof.get('host')
            and proof.get('ticker') == source['ticker'] and bool(proof.get('root_fetch_id'))
            and re.fullmatch(r'[a-f0-9]{64}', proof.get('root_sha256', '')) is not None
            and re.fullmatch(r'[0-9]{10}', proof.get('cik', '')) is not None
            and proof.get('cik') == result.get('registry_entry', {}).get('cik')
            and result.get('verification', {}).get('issuer_status') == 'ISSUER_ATTRIBUTION_MATCH')
