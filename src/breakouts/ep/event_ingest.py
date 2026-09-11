"""Bounded current-news and official-source collection for the AI commentary worker."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from src.data.sec_company_index import INDEX_URL, SecCompanyIndexClient
from src.data.sec_attachments import SecDisclosureClient
from src.utils.io import atomic_save_json
from .discovery import OfficialSourceDiscovery
from .models import EpSettings, NEW_YORK, ticker
from .provider import FmpEpProvider
from .service import EpRadar
from .store import EpStore


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


def ingest(config, *, clock=lambda: datetime.now(timezone.utc), provider=None, index_client=None, disclosure_client=None):
    now = clock()
    store = EpStore(config.database)
    day = now.astimezone(NEW_YORK).date()
    contact = os.getenv("SEC_CONTACT_EMAIL")
    index_client = index_client or SecCompanyIndexClient(contact_email=contact, max_requests=3, deadline_seconds=30)
    cache = Path(config.output_directory) / "sec_company_index.json"
    cached = None
    if cache.is_file() and cache.stat().st_size <= 3_000_000:
        cached = json.loads(cache.read_text())
        received = datetime.fromisoformat(cached["received_at"])
        if received.tzinfo is None or not timedelta(0) <= now - received < timedelta(hours=24):
            cached = None
    if cached is None:
        fetched = index_client.fetch_json(INDEX_URL)
        if fetched["status"] != "FETCHED":
            return {"collection_status": "NOT_STARTED", "source_status": "COMPANY_INDEX_UNAVAILABLE",
                    "index_status": fetched["status"], "market_complete": False}
        payload = json.loads(fetched["json"])
        registry_for_candidates(payload, [])
        cached = {"received_at": now.isoformat(), "payload": payload, "source_url": INDEX_URL}
        atomic_save_json(cached, cache)
    priority_symbols = {ticker(row["ticker"]) for row in cached["payload"].values()}
    report = EpRadar(store, provider or FmpEpProvider(), EpSettings(max_pages=2, max_profiles=20,
                    max_requests=30, deadline_seconds=90, include_etfs=False), clock=clock,
                    priority_symbols=priority_symbols).collect(
                        (day - timedelta(days=3)).isoformat(), day.isoformat())
    summary = {"run_id": report["run_id"], "collection_status": report["status"],
               "candidate_count": len(report["candidates"]), "market_complete": False}
    symbols = [c["ticker"] for c in report["candidates"] if c["status"] != "EXCLUDED"]
    registry, missing = registry_for_candidates(cached["payload"], symbols)
    disclosure_client = disclosure_client or SecDisclosureClient(contact_email=contact, max_requests=20, deadline_seconds=90)
    sources = OfficialSourceDiscovery(store, disclosure_client, registry, max_documents=3,
                                      max_filings=2, max_exhibits=2, skip_existing=True,
                                      prioritize_current=True, clock=clock).run(report["run_id"])
    return {**summary, "registered_candidates": len(registry["issuers"]), "unmapped_symbols": missing,
            "source_status": sources["status"], "source_counts": sources["summary"].get("counts", {}),
            "sec_requests": index_client.requests + disclosure_client.requests}
