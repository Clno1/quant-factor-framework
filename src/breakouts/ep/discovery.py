"""Bounded SEC source discovery from registered issuers, independent of price loops."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from .models import NEW_YORK, digest, ticker, timestamp
from .sec_source import SEC_PARSER_VERSION, parse_sec_attachment
from .source_verifier import verify_document


def load_registry(path: Path) -> dict:
    with path.open("rb") as handle:
        raw = handle.read(100_001)
    if len(raw) > 100_000:
        raise ValueError("Issuer registry exceeds 100 KB")
    registry = json.loads(raw)
    if not isinstance(registry, dict) or registry.get("version") != "ep-issuer-registry-v1":
        raise ValueError("Unsupported issuer registry")
    issuers = registry.get("issuers")
    if not isinstance(issuers, dict) or len(issuers) > 5000:
        raise ValueError("Invalid issuer registry")
    for symbol, row in issuers.items():
        if symbol != ticker(symbol) or not isinstance(row, dict):
            raise ValueError("Invalid issuer registry entry")
        if not re.fullmatch(r"[0-9]{10}", str(row.get("cik", ""))):
            raise ValueError("Registry CIK must contain ten digits")
        if not isinstance(row.get("name"), str) or not row["name"].strip():
            raise ValueError("Issuer name required")
        evidence = urlsplit(str(row.get("evidence_url", "")))
        if evidence.scheme != "https" or evidence.netloc not in {"www.sec.gov", "data.sec.gov"}:
            raise ValueError("Official registry evidence required")
    return registry


def select_filings(payload: dict, symbol: str, cik: str, event_day: str, now: datetime) -> list[dict]:
    if not isinstance(payload, dict) or str(payload.get("cik", "")).zfill(10) != cik:
        raise ValueError("SEC_CIK_MISMATCH")
    symbols = payload.get("tickers")
    if not isinstance(symbols, list) or symbol not in {ticker(s) for s in symbols}:
        raise ValueError("SEC_TICKER_MISMATCH")
    recent = payload.get("filings", {}).get("recent")
    keys = ("accessionNumber", "filingDate", "acceptanceDateTime", "form", "primaryDocument")
    if not isinstance(recent, dict) or any(not isinstance(recent.get(k), list) for k in keys):
        raise ValueError("SEC_RECENT_SCHEMA_UNSUPPORTED")
    if len({len(recent[k]) for k in keys}) != 1:
        raise ValueError("SEC_RECENT_COLUMNS_MISALIGNED")
    start = date.fromisoformat(event_day)
    end = start + timedelta(days=3)
    output = []
    for values in zip(*(recent[k] for k in keys)):
        row = dict(zip(keys, values))
        if row["form"] not in {"8-K", "6-K", "8-K/A", "6-K/A"}:
            continue
        day = date.fromisoformat(row["filingDate"])
        if not start <= day <= end:
            continue
        accepted = datetime.fromisoformat(row["acceptanceDateTime"].replace("Z", "+00:00"))
        if timestamp(accepted) > timestamp(now):
            continue
        if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", row["accessionNumber"]):
            raise ValueError("SEC_ACCESSION_INVALID")
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:htm|html)", row["primaryDocument"]):
            continue
        accession = row["accessionNumber"].replace("-", "")
        row["url"] = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{row['primaryDocument']}"
        output.append(row)
    return sorted(output, key=lambda r: (r["filingDate"], r["acceptanceDateTime"], r["accessionNumber"]))


def exhibit_links(raw: bytes, primary_url: str) -> list[dict]:
    from lxml import etree, html
    try:
        root = html.fromstring(raw, parser=html.HTMLParser(no_network=True))
    except etree.LxmlError:
        raise ValueError("PRIMARY_FILING_PARSE_ERROR") from None
    parent = urlsplit(primary_url)
    folder = parent.path.rsplit("/", 1)[0] + "/"
    found = {}
    for anchor in root.xpath("//a[@href]"):
        rows = anchor.xpath("ancestor::tr[1]")
        label = " ".join(" ".join((rows[0] if rows else anchor).itertext()).split())
        if not re.search(r"(?<![0-9])(?:EX-)?99\.[0-9]+\b", label, re.I):
            continue
        url = urljoin(primary_url, anchor.get("href"))
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.netloc != "www.sec.gov" or parts.query or
                parts.path.rsplit("/", 1)[0] + "/" != folder or
                not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:htm|html|pdf)", parts.path.rsplit("/", 1)[1])):
            continue
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        found[url] = {"url": url, "label": label[:500]}
    return list(found.values())


class OfficialSourceDiscovery:
    def __init__(self, store, client, registry: dict, *, max_documents=10, max_filings=3, max_exhibits=3,
                 clock=lambda: datetime.now(timezone.utc), skip_existing=False, prioritize_current=False):
        for value in (max_documents, max_filings, max_exhibits):
            if type(value) is not int or not 1 <= value <= 20:
                raise ValueError("Discovery limits must be integers between 1 and 20")
        self.store, self.client, self.registry = store, client, registry
        self.max_documents, self.max_filings, self.max_exhibits = max_documents, max_filings, max_exhibits
        self.clock = clock
        self.memo = {}
        self.skip_existing = skip_existing
        self.prioritize_current = prioritize_current

    def _fetch(self, batch, url, kind="html"):
        self.client.validate_url(url)
        key = (url, kind)
        if key in self.memo:
            return self.memo[key]
        cached = self.store.cached_fetch(url, self.clock())
        ttl = timedelta(minutes=15) if kind == "json" else timedelta(hours=24)
        reusable = {"FETCHED", "SOURCE_HTTP_401", "SOURCE_HTTP_403", "SOURCE_HTTP_429", "ROBOTS_DISALLOWED", "SOURCE_TIMEOUT"}
        if cached and cached["result"].get("status") in reusable and cached["result"].get("kind") == kind:
            if cached["result"]["status"] != "FETCHED":
                ttl = timedelta(minutes=15)
            age = self.clock() - datetime.fromisoformat(cached["result"]["received_at"])
            if timedelta(0) <= age < ttl:
                result = {**cached["result"], "cache_used": True, "reused_from_fetch_id": cached["fetch_id"],
                          "body_fetch_id": cached["body_fetch_id"]}
                fetch_id = self.store.save_fetch(batch, url, result, self.clock())
                self.memo[key] = (result, cached["raw"], fetch_id)
                return self.memo[key]
        fetched = self.client.fetch_json(url) if kind == "json" else self.client.fetch(url)
        raw = fetched.pop(kind, None)
        result = {**fetched, "kind": kind, "cache_used": False}
        fetch_id = self.store.save_fetch(batch, url, result, self.clock(), raw=raw)
        self.memo[key] = (result, raw, fetch_id)
        return self.memo[key]

    def _resolve(self, batch, candidate, event):
        row = self.registry["issuers"][candidate["ticker"]]
        url = f"https://data.sec.gov/submissions/CIK{row['cik']}.json"
        trace, matches, incomplete = [], [], []
        result, raw, fetch_id = self._fetch(batch, url, "json")
        trace.append({"stage": "SUBMISSIONS", "fetch_id": fetch_id, "url": url, "status": result["status"]})
        base = {"discovery_steps": trace, "registry_entry": row, "registry_revision": digest(self.registry),
                "source_route": "SEC_AUTO_DISCOVERY", "coverage": "REGISTERED_ISSUER_RECENT_FILINGS_ONLY"}
        if result["status"] != "FETCHED":
            return {**base, "status": result["status"]}, None, None
        try:
            event_day = datetime.fromisoformat(event["published_at"]).astimezone(NEW_YORK).date().isoformat()
            filings = select_filings(json.loads(raw), candidate["ticker"], row["cik"], event_day, self.clock())
        except (ValueError, TypeError, KeyError, AttributeError):
            return {**base, "status": "SEC_SUBMISSIONS_UNVERIFIED"}, None, None
        base["issuer_linkage"] = "REGISTERED_CIK_AND_CURRENT_SEC_TICKER_MATCH"
        base["filings_in_window"] = len(filings)
        base["window_calendar_days"] = 3
        if len(filings) > self.max_filings:
            incomplete.append("FILING_BUDGET_EXCEEDED")
        for filing in filings[:self.max_filings]:
            result, raw, fid = self._fetch(batch, filing["url"])
            trace.append({"stage": "PRIMARY_FILING", "fetch_id": fid, "url": filing["url"], "status": result["status"],
                          "form": filing["form"], "acceptanceDateTime": filing["acceptanceDateTime"]})
            if result["status"] != "FETCHED":
                incomplete.append(result["status"])
                continue
            try:
                links = exhibit_links(raw, filing["url"])
            except ValueError:
                incomplete.append("PRIMARY_FILING_PARSE_ERROR")
                continue
            if not links:
                incomplete.append("EXHIBIT_LINK_NOT_FOUND")
            if len(links) > self.max_exhibits:
                incomplete.append("EXHIBIT_BUDGET_EXCEEDED")
            for link in links[:self.max_exhibits]:
                if link["url"].lower().endswith(".pdf"):
                    trace.append({"stage": "EXHIBIT", **link, "status": "PDF_REQUIRES_SEPARATE_WORKER"})
                    incomplete.append("PDF_REQUIRES_SEPARATE_WORKER")
                    continue
                response, body, eid = self._fetch(batch, link["url"])
                step = {"stage": "EXHIBIT", "fetch_id": eid, **link, "status": response["status"]}
                trace.append(step)
                if response["status"] != "FETCHED":
                    incomplete.append(response["status"])
                    continue
                try:
                    parsed = parse_sec_attachment(body, link["url"])
                    verification = verify_document(event, parsed, candidate["identity"])
                    step.update(parse_status=parsed["status"], verification=verification, title=parsed["title"])
                except (ValueError, TypeError):
                    step["parse_status"] = "UNSUPPORTED_SEC_ATTACHMENT"
                    incomplete.append(step["parse_status"])
                    continue
                if parsed["status"] != "EXTRACTED":
                    incomplete.append(parsed["status"])
                if verification["status"] == "DOCUMENT_MATCHED":
                    if not any(match[0] == link["url"] for match in matches):
                        matches.append((link["url"], body, parsed, verification, response, eid))
        base["incomplete_reasons"] = sorted(set(incomplete))
        if len(matches) > 1:
            return {**base, "status": "AMBIGUOUS_MATCH_REVIEW_REQUIRED"}, None, None
        if len(matches) == 1:
            url, raw, parsed, verification, response, eid = matches[0]
            return {**base, "status": "DOCUMENT_MATCHED", "verification": verification, "final_url": url,
                    "retrieved_at": response["received_at"], "raw_sha256": response["raw_sha256"],
                    "fetch_id": eid, "body_characters": parsed["characters"], "paragraph_count": len(parsed["paragraphs"]),
                    "text_revision": parsed["text_revision"], "parser_version": SEC_PARSER_VERSION,
                    "search_complete": not incomplete}, raw, parsed
        status = "NO_SUPPORTED_RECENT_FILING_IN_WINDOW" if not filings else "NO_MATCHING_EXHIBIT_IN_FETCHED_SCOPE"
        if incomplete:
            status = "SOURCE_DISCOVERY_INCOMPLETE"
        return {**base, "status": status}, None, None

    def run(self, run_id=None, *, symbol=None):
        from src.utils.file_lock import file_lock
        if self.store.read_only:
            raise ValueError("Discovery requires writable EP evidence store")
        with file_lock(self.store.path.with_suffix(".sources.lock")):
            return self._run(run_id, symbol)

    def _run(self, run_id, symbol):
        report = self.store.report(run_id)
        if report["status"] not in {"PARTIAL", "COMPLETE_OBSERVATION"}:
            raise ValueError("A finished collection is required before discovery")
        self.memo = {}
        config = {"version": "ep-sec-discovery-v1", "registry_revision": digest(self.registry),
                  "max_documents": self.max_documents, "max_filings": self.max_filings, "max_exhibits": self.max_exhibits,
                  "max_http_requests": self.client.max_requests, "ticker_scope": symbol,
                  "mode": "SHADOW", "delivery": "DISABLED_SHADOW_ONLY", "llm": "NOT_CONFIGURED"}
        config["prioritize_current"] = self.prioritize_current
        batch = self.store.start_source_run(report["run_id"], config, self.clock())
        count, attempted, initial = Counter(), 0, self.client.requests
        existing = set()
        if self.skip_existing:
            with self.store.connection() as db:
                existing = {tuple(row) for row in db.execute("""SELECT DISTINCT a.document_id, a.revision_id
                    FROM ep_source_attempts a JOIN ep_source_runs r USING(batch_id)
                    JOIN ep_observations o ON o.document_id=a.document_id AND o.revision_id=a.revision_id
                    WHERE o.run_id=? AND r.finished_at IS NOT NULL AND r.finished_at<=? AND a.observed_at<=?
                    AND json_extract(a.payload_json,'$.status')='DOCUMENT_MATCHED'
                    AND json_extract(a.payload_json,'$.issuer_linkage')='REGISTERED_CIK_AND_CURRENT_SEC_TICKER_MATCH'""",
                    (report["run_id"], timestamp(self.clock()), timestamp(self.clock())))}
        candidates = report["candidates"]
        window_start = None
        if self.prioritize_current:
            from .catalyst import event_window
            window_start = event_window(self.clock())["start"]
            with self.store.connection() as db:
                last_attempt = dict(db.execute("""SELECT a.ticker, MAX(a.observed_at)
                    FROM ep_source_attempts a JOIN ep_source_runs r USING(batch_id)
                    WHERE r.finished_at IS NOT NULL AND a.observed_at>=? AND a.observed_at<=?
                    AND json_extract(a.payload_json,'$.discovery_steps') IS NOT NULL
                    GROUP BY a.ticker""", (timestamp(window_start), timestamp(self.clock()))))
            def queue_key(candidate):
                events = [e for e in candidate["events"] if "url" in e["evidence"]]
                hints = [e for e in events if e["event_type_hint"] not in {"UNKNOWN", "LEGAL_NOTICE"}]
                latest = max((datetime.fromisoformat(e["published_at"]).timestamp() for e in hints or events
                              if e.get("published_at")), default=0)
                return (candidate.get("identity") is None, not bool(hints),
                        last_attempt.get(candidate["ticker"], ""), -latest, candidate["ticker"])
            candidates = sorted(candidates, key=queue_key)
        attempted_symbols = set()
        try:
            for candidate in candidates:
                if symbol and candidate["ticker"] != symbol:
                    continue
                seen = set()
                events = sorted(candidate["events"], key=lambda e: e["published_at"] or "", reverse=True) if self.prioritize_current else candidate["events"]
                for event in events:
                    if "url" not in event["evidence"] or (event["document_id"], event["revision_id"]) in seen:
                        continue
                    seen.add((event["document_id"], event["revision_id"]))
                    if (event["document_id"], event["revision_id"]) in existing:
                        count["EXISTING_VERIFIED_SOURCE"] += 1
                        continue
                    raw, parsed = None, None
                    if candidate["status"] == "EXCLUDED" or event["event_type_hint"] == "LEGAL_NOTICE":
                        result = {"status": "EXCLUDED_FROM_SOURCE_QUEUE"}
                    elif self.prioritize_current and candidate.get("identity") is None:
                        result = {"status": "SECURITY_IDENTITY_PENDING"}
                    elif window_start is not None and (not event.get("published_at") or not window_start < datetime.fromisoformat(event["published_at"]) <= self.clock()):
                        result = {"status": "OUTSIDE_CURRENT_PROVIDER_WINDOW"}
                    elif candidate["ticker"] not in self.registry["issuers"]:
                        result = {"status": "ISSUER_NOT_REGISTERED"}
                    elif self.prioritize_current and candidate["ticker"] in attempted_symbols:
                        result = {"status": "ISSUER_CAPACITY_DEFERRED"}
                    elif attempted >= self.max_documents:
                        result = {"status": "SOURCE_DOCUMENT_BUDGET_EXCEEDED"}
                    else:
                        attempted += 1
                        attempted_symbols.add(candidate["ticker"])
                        result, raw, parsed = self._resolve(batch, candidate, event)
                    result.update(original_url=event["evidence"]["url"], llm="NOT_CONFIGURED", delivery="DISABLED_SHADOW_ONLY")
                    self.store.save_source_attempt(batch, candidate["ticker"], event, result, self.clock(), raw=raw, parsed=parsed)
                    count[result["status"]] += 1
            status = "SOURCE_PASS_COMPLETED" if count and set(count) <= {"DOCUMENT_MATCHED", "EXCLUDED_FROM_SOURCE_QUEUE", "EXISTING_VERIFIED_SOURCE"} else (
                "PARTIAL_SOURCES" if count else "NO_SOURCE_TARGETS")
            self.store.finish_source_run(batch, {"status": status, "counts": dict(count), "attempted_documents": attempted,
                "http_requests": self.client.requests - initial, "ratings_enabled": False,
                "unique_source_urls": len(self.memo),
                "cached_source_urls": sum(bool(value[0].get("cache_used")) for value in self.memo.values()),
                "completeness_claimed": False, "delivery": "DISABLED_SHADOW_ONLY"}, self.clock())
        except Exception as exc:
            self.store.finish_source_run(batch, {"status": "FAILED", "error_code": type(exc).__name__}, self.clock())
            raise
        return self.store.source_report(report["run_id"])
