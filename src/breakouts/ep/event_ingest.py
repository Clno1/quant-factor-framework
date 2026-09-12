"""Bounded current-news and official-source collection for the AI commentary worker."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from src.data.sec_company_index import INDEX_URL, SecCompanyIndexClient
from src.data.sec_attachments import SecDisclosureClient
from src.data.public_articles import SourceAccessError
from src.utils.io import atomic_save_json
from .discovery import OfficialSourceDiscovery
from .models import EpSettings, NEW_YORK, ticker
from .provider import FmpEpProvider
from .service import EpRadar
from .store import EpStore
from .identity import load_identity_snapshot
from .pipeline import process_identities, process_sources, queue_path, seed
from .queue import PipelineQueue


def registry_for_candidates(payload, symbols):
    if not isinstance(payload, dict) or not 1 <= len(payload) <= 30_000:
        raise ValueError("SEC_COMPANY_INDEX_SCHEMA_INVALID")
    wanted, found, ambiguous = set(symbols), {}, set()
    for row in payload.values():
        if not isinstance(row, dict) or type(row.get("cik_str")) is not int or not 0 < row["cik_str"] < 10**10:
            raise ValueError("SEC_COMPANY_INDEX_ROW_INVALID")
        symbol = ticker(row.get("ticker"))
        name = row.get("title")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("SEC_COMPANY_INDEX_NAME_INVALID")
        if symbol not in wanted:
            continue
        item = {"cik": str(row["cik_str"]).zfill(10), "name": name, "evidence_url": INDEX_URL}
        if symbol in found and found[symbol]["cik"] != item["cik"]:
            ambiguous.add(symbol)
        found[symbol] = item
    for symbol in ambiguous:
        found.pop(symbol, None)
    return {"version": "ep-issuer-registry-v1", "issuers": found}, sorted(wanted - found.keys())


def ingest(config, *, clock=lambda: datetime.now(timezone.utc), provider=None, index_client=None,
           disclosure_client=None, identity_snapshot=None):
    from src.utils.file_lock import file_lock
    with file_lock(Path(queue_path(config)).with_suffix('.ingest.lock')):
        return _ingest(config, clock=clock, provider=provider, index_client=index_client,
                       disclosure_client=disclosure_client, identity_snapshot=identity_snapshot)


def _ingest(config, *, clock, provider, index_client, disclosure_client, identity_snapshot):
    now = clock()
    store = EpStore(config.database)
    queue = PipelineQueue(queue_path(config))
    queue.expire(now)
    watch = {'status': 'WATCH_INPUT_NOT_CONFIGURED'}
    if config.watch_input_path:
        from .watch import ingest_watch_file
        try:
            watch = ingest_watch_file(queue, config.watch_input_path, now)
        except (ValueError, OSError):
            watch = {'status': 'WATCH_INPUT_UNAVAILABLE'}
    provider = provider or FmpEpProvider()
    identity_snapshot = identity_snapshot or load_identity_snapshot(now,
        catalog_path=config.identity_catalog_path or None, snapshot_root=config.identity_snapshot_root or None,
        source_root=config.identity_source_root or None)
    price_discovery = {'status': 'DISABLED'}
    price_news = {'status': 'DISABLED'}
    if config.price_discovery_enabled and not config.independent_consumers_enabled:
        from .price_discovery import PriceStore, discover
        from .price_news import collect_price_news
        try:
            price_discovery = discover(queue, PriceStore(config.price_database), identity_snapshot, provider, config, clock=clock)
        except Exception as exc:
            price_discovery = {'status': 'PRICE_DISCOVERY_FAILED', 'error_type': type(exc).__name__}
        try:
            price_news = collect_price_news(queue, store, provider, config, clock=clock)
        except Exception as exc:
            price_news = {'status': 'PRICE_NEWS_FAILED', 'error_type': type(exc).__name__}
    day = now.astimezone(NEW_YORK).date()
    cached, index_status, index_client = load_issuer_index(config, clock=clock, index_client=index_client)
    priority_symbols = {ticker(row['ticker']) for row in cached['payload'].values()} if cached else set()
    report = EpRadar(store, provider, EpSettings(max_pages=config.news_pages_per_feed, max_profiles=0,
                    max_requests=2 * config.news_pages_per_feed + 4, deadline_seconds=90, include_etfs=False), clock=clock,
                    priority_symbols=priority_symbols, identity_snapshot=identity_snapshot, pipeline=queue).collect(
                        (day - timedelta(days=3)).isoformat(), day.isoformat())
    summary = {"run_id": report["run_id"], "collection_status": report["status"],
               "candidate_count": len(report["candidates"]), "market_complete": False, 'watch': watch,
               'price_discovery': price_discovery, 'price_news': price_news}
    symbols = [c["ticker"] for c in report["candidates"] if c["status"] != "EXCLUDED"]
    registry, missing = registry_for_candidates(cached['payload'], symbols) if cached else (
        {'version': 'ep-issuer-registry-v1', 'issuers': {}}, symbols)
    # Keep identity and source work after the collection window advances.
    if cached:
        registry, _ = registry_for_candidates(cached['payload'], priority_symbols)
    seed(queue, report, clock())
    identities = ({'status': 'OWNED_BY_INDEPENDENT_CONSUMERS'} if config.independent_consumers_enabled else
        process_identities(queue, store, provider, identity_snapshot, config, clock=clock))
    if config.independent_consumers_enabled:
        source_counts, source_status, source_requests, public_requests = {}, 'OWNED_BY_INDEPENDENT_CONSUMERS', 0, 0
    else:
        source_counts, source_status, source_requests, public_requests = consume_sources(
            config, queue, store, registry, clock=clock, disclosure_client=disclosure_client)
    queue.save_checkpoint('discovery:last', {**summary, 'coverage': report['summary'].get('coverage', []),
        'identity_source': identity_snapshot.provenance, 'identity_diagnostics': identity_snapshot.diagnostics,
        'index_status': index_status, 'source_status': source_status}, clock())
    atomic_save_json(queue.summary(), Path(config.output_directory) / 'pipeline.json')
    return {**summary, "registered_candidates": len(set(symbols) & registry["issuers"].keys()), "unmapped_symbols": missing,
            'unmapped_symbols_scope': 'SEC_COMPANY_INDEX_ONLY_NOT_SECURITY_IDENTITY',
            'sec_index_unmapped_symbols': missing,
            'identity_processing': identities, 'identity_source': identity_snapshot.provenance,
            'index_status': index_status, 'queue': queue.summary(),
            "source_status": source_status, "source_counts": source_counts,
            "sec_requests": getattr(index_client, 'requests', 0) + source_requests,
            'public_source_requests': public_requests}


def load_issuer_index(config, *, clock, index_client=None):
    now = clock()
    contact = os.getenv("SEC_CONTACT_EMAIL")
    cache = Path(config.output_directory) / "sec_company_index.json"
    cached = None
    index_status = 'UNAVAILABLE'
    try:
        if cache.is_file() and cache.stat().st_size <= 3_000_000:
            cached = json.loads(cache.read_text())
            received = datetime.fromisoformat(cached["received_at"])
            if received.tzinfo is None or not timedelta(0) <= now - received < timedelta(hours=24):
                cached = None
            elif cached.get('source_url') != INDEX_URL:
                cached = None
            else:
                registry_for_candidates(cached['payload'], [])
        if cached is None:
            index_client = index_client or SecCompanyIndexClient(contact_email=contact, max_requests=3, deadline_seconds=30)
            fetched = index_client.fetch_json(INDEX_URL)
            index_status = fetched['status']
            if fetched['status'] == 'FETCHED':
                payload = json.loads(fetched['json'])
                registry_for_candidates(payload, [])
                cached = {'received_at': now.isoformat(), 'payload': payload, 'source_url': INDEX_URL}
                atomic_save_json(cached, cache)
        else:
            index_status = 'CACHED_UNDER_24H'
    except (ValueError, KeyError, TypeError, OSError, SourceAccessError):
        cached, index_status = None, 'COMPANY_INDEX_NOT_AVAILABLE'
    return cached, index_status, index_client


def consume_sources(config, queue, store, registry, *, clock, disclosure_client=None):
    contact = os.getenv('SEC_CONTACT_EMAIL')
    public = None
    source_counts = {}
    try:
        try:
            disclosure_client = disclosure_client or SecDisclosureClient(contact_email=contact,
                max_requests=30, deadline_seconds=getattr(config, 'source_deadline_seconds', 110), max_bytes=5_000_000)
        except ValueError:
            disclosure_client = None
        resolver = OfficialSourceDiscovery(store, disclosure_client, registry, max_documents=config.source_jobs_per_cycle,
                                           max_filings=2, max_exhibits=2, clock=clock)
        if config.official_registry_path:
            from urllib.parse import urlsplit
            from src.data.public_articles import PublicArticleClient
            from .official_sources import OfficialSourceRouter, load_official_registry
            official = load_official_registry(config.official_registry_path)
            hosts = {h for row in official['issuers'].values()
                     for h in (urlsplit(row['root_url']).hostname, row['ir_host'])}
            public = PublicArticleClient(allowed_hosts=hosts, max_requests=30, deadline_seconds=getattr(config, 'source_deadline_seconds', 110), max_bytes=5_000_000)
            resolver = OfficialSourceRouter(store, disclosure_client, registry, public, official,
                max_documents=config.source_jobs_per_cycle, max_filings=2, max_exhibits=2, clock=clock)
        elif disclosure_client is None:
            raise ValueError('SOURCE_ACCESS_NOT_CONFIGURED')
        source_counts = process_sources(queue, store, resolver, config, clock=clock)
        source_status = 'QUEUED_SOURCE_PASS_COMPLETED'
    except (ValueError, OSError, SourceAccessError):
        source_status = 'SOURCE_ACCESS_NOT_CONFIGURED_JOBS_RETAINED'
    return source_counts, source_status, getattr(disclosure_client, 'requests', 0), getattr(public, 'requests', 0)
