#!/usr/bin/env python3
"""Explicit known-case IR and authorized FMP audit, isolated from production."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.breakouts.ep.source_verifier import parse_article, verify_document
from src.data.public_articles import PublicArticleClient


IR = [
    ("GTLB", "GitLab Reports Second Quarter Fiscal Year 2027 Financial Results",
     "https://ir.gitlab.com/news/news-details/2026/GitLab-Reports-Second-Quarter-Fiscal-Year-2027-Financial-Results/default.aspx"),
    ("SNOW", "Snowflake Reports Financial Results for the Second Quarter of Fiscal 2027",
     "https://investors.snowflake.com/news/news-details/2026/Snowflake-Reports-Financial-Results-for-the-Second-Quarter-of-Fiscal-2027/default.aspx"),
    ("AFRM", "Affirm reports fourth fiscal quarter 2026 results",
     "https://investors.affirm.com/news-releases/news-release-details/affirm-reports-fourth-fiscal-quarter-2026-results"),
    ("NYAX", "Nayax Enters into Definitive Agreement to Acquire IPS Group, a Leading Smart Parking Technology Provider",
     "https://ir.nayax.com/news/news-details/2026/Nayax-Enters-into-Definitive-Agreement-to-Acquire-IPS-Group-a-Leading-Smart-Parking-Technology-Provider/default.aspx"),
    ("DELL", "Dell Technologies Delivers Second Quarter Fiscal 2027 Financial Results",
     "https://investors.delltechnologies.com/news-releases/news-release-details/dell-technologies-delivers-second-quarter-fiscal-2027-financial"),
    ("SAIC", "SAIC Announces Second Quarter of Fiscal Year 2027 Results",
     "https://investors.saic.com/news-releases/news-release-details/saic-announces-second-quarter-fiscal-year-2027-results"),
    ("PLAB", "Photronics Reports Third Quarter 2026 Results",
     "https://photronicsinc.gcs-web.com/news-releases/news-release-details/photronics-reports-third-quarter-2026-results"),
    ("ANF", "Abercrombie & Fitch Co. Reports Second Quarter Fiscal 2026 Results",
     "https://abercrombieandfitchcompany.gcs-web.com/news-releases/news-release-details/abercrombie-fitch-co-reports-second-quarter-fiscal-2026-results"),
]


def now():
    return datetime.now(timezone.utc).isoformat()


def transcript_period(row):
    values = []
    for field in ("quarter", "period"):
        value = str(row.get(field) or "").upper()
        if value:
            value = value.removeprefix("Q")
            if value not in {"1", "2", "3", "4"}:
                return None
            values.append(int(value))
    return values[0] if values and len(set(values)) == 1 else None


def inspect_html(raw, expected_title):
    from lxml import html
    parsed = parse_article(raw)
    root = html.fromstring(raw)
    containers = [{"tag": node.tag, "id": node.get("id"), "class": node.get("class"),
                   "characters": len(node.text_content())} for node in root.xpath('//*[@id or @class]')
                  if any(token in ((node.get("id") or "") + " " + (node.get("class") or "")).lower()
                         for token in ("body", "release", "detail"))]
    return {"parser": {key: value for key, value in parsed.items() if key != "paragraphs"},
            "paragraph_count": len(parsed["paragraphs"]),
            "verification": verify_document({"evidence": {"title": expected_title}}, parsed, None),
            "candidate_containers": containers[:40]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fmp", action="store_true", help="At most six read-only authenticated requests")
    args = parser.parse_args()
    if args.db.exists() or args.output.exists():
        parser.error("Choose new output/database paths; audit history is never overwritten")
    connection = sqlite3.connect(args.db)
    connection.execute("CREATE TABLE audit_raw (name TEXT PRIMARY KEY, received_at TEXT, sha256 TEXT, raw BLOB)")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": now(), "scope": "EIGHT_KNOWN_EVENT_IR_URLS_NOT_AUTOMATED_DISCOVERY",
              "database": str(args.db.resolve()), "ir": [], "fmp": [],
              "sec": {"status": "DEFERRED_REQUIRES_VALID_CONTACT_USER_AGENT",
                      "prior_20260906_metadata_success_is_not_new_fulltext_success": True},
              "llm_calls": 0, "discord_messages": 0, "production_changes": False}

    def save():
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    client = PublicArticleClient(allowed_hosts={urlsplit(row[2]).hostname for row in IR},
                                 max_requests=32, deadline_seconds=240, timeout_seconds=12)
    try:
        for symbol, title, url in IR:
            before = client.requests
            result = client.fetch(url)
            raw = result.pop("html", None)
            entry = {"ticker": symbol, "expected_title": title, "url": url, **result,
                     "http_requests": client.requests - before, "completeness_verified": False}
            if raw is not None:
                connection.execute("INSERT INTO audit_raw VALUES (?, ?, ?, ?)",
                                   (symbol + "_IR", now(), hashlib.sha256(raw).hexdigest(), raw))
                connection.commit()
                try:
                    entry.update(inspect_html(raw, title))
                except Exception as exc:
                    entry["parse_error_type"] = type(exc).__name__
            report["ir"].append(entry)
            save()
            print(json.dumps({"ticker": symbol, "status": result["status"],
                              "parsed": entry.get("parser", {}).get("status")}), flush=True)
        report["ir_http_requests"] = client.requests
        if args.fmp:
            from src.data.fmp import _request, get_api_key
            key = get_api_key()
            jobs = [("TRANSCRIPT", symbol, "/earning-call-transcript", {"symbol": symbol, "year": year, "quarter": quarter})
                    for symbol, year, quarter in (("GTLB", 2027, 2), ("SNOW", 2027, 2), ("AFRM", 2026, 4), ("DELL", 2027, 2))]
            jobs += [("PRESS_RELEASE", symbol, "/news/press-releases", {"symbols": symbol, "from": "2026-08-25",
                      "to": "2026-09-03", "limit": 5}) for symbol in ("GTLB", "AFRM")]
            for kind, symbol, endpoint, params in jobs:
                entry = {"kind": kind, "ticker": symbol, "endpoint": endpoint, "params": params, "requested_at": now(),
                         "premarket_availability_verified": False, "redistribution_license_verified": False}
                stop = False
                try:
                    response = _request(endpoint, params, timeout=15, retry=0, rate_limit_passthrough=True)
                    raw = response.content
                    if len(raw) > 5_000_000:
                        raise ValueError("Oversized response")
                    rows = response.json()
                    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                        raise ValueError("Expected record list")
                    if key.encode() in raw:
                        raise ValueError("Response unexpectedly contains credential")
                    entry.update(status="OK" if rows else "EMPTY", http_status=response.status_code,
                                 row_count=len(rows), rows=[])
                    for row in rows:
                        body = row.get("content" if kind == "TRANSCRIPT" else "text")
                        body = body if isinstance(body, str) else ""
                        entry["rows"].append({k: row.get(k) for k in ("symbol", "year", "quarter", "period", "date", "publishedDate", "title")})
                        entry["rows"][-1].update(text_characters=len(body), text_sha256=hashlib.sha256(body.encode()).hexdigest(),
                            expected_identity=(row.get("symbol") == symbol),
                            expected_period=(str(row.get("year")) == str(params.get("year")) and
                                             transcript_period(row) == params.get("quarter")) if kind == "TRANSCRIPT" else None)
                    connection.execute("INSERT INTO audit_raw VALUES (?, ?, ?, ?)",
                                       (symbol + "_" + kind, now(), hashlib.sha256(raw).hexdigest(), raw))
                    connection.commit()
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    entry.update(status="FAILED", http_status=status, error_type=type(exc).__name__)
                    stop = status in {401, 403, 429}
                entry["received_at"] = now()
                report["fmp"].append(entry)
                save()
                print(json.dumps({"ticker": symbol, "kind": kind, "status": entry["status"]}), flush=True)
                if stop:
                    report["fmp_stop_reason"] = "AUTH_OR_RATE_REFUSAL_NO_RETRY"
                    break
                time.sleep(1)
        report["finished_at"] = now()
        save()
    finally:
        connection.close()


if __name__ == "__main__":
    main()
