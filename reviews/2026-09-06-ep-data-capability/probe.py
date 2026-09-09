#!/usr/bin/env python3
"""Bounded, read-only FMP capability audit; not a production data adapter.

Uses the same stable host and apikey header as src/data/fmp.py. Only stdlib
is required so the audit does not change the application's environment.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, time as wall_time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request


CASES = [
    ("NYAX", "2026-08-25", "08:15", "Moderate", 11.8),
    ("PLAB", "2026-08-26", "08:14", "Strong", 17.5),
    ("ANF", "2026-08-26", "08:14", "Moderate", 11.0),
    ("OKTA", "2026-08-27", "08:15", "Strong", 17.3),
    ("CRWD", "2026-08-27", "08:15", "Strong", 9.4),
    ("DG", "2026-08-27", "08:15", "Strong", 7.5),
    ("VEEV", "2026-08-27", "09:36", "Strong", 14.7),
    ("ESTC", "2026-08-28", "08:14", "Strong", 22.4),
    ("AFRM", "2026-08-28", "08:14", "Strong", 12.8),
    ("GAP", "2026-08-28", "08:14", "Moderate", 16.0),
    ("SAIC", "2026-08-31", "08:13", "Moderate", 8.2),
    ("GTLB", "2026-09-02", "08:13", "Strong", 22.2),
    ("DELL", "2026-09-02", "08:13", "Strong", 8.8),
    ("SNOW", "2026-09-03", "08:13", "Strong", 23.0),
    ("NTSK", "2026-09-03", "08:13", "Strong", 12.9),
    ("AGX", "2026-09-03", "08:13", "Strong_thin", 5.5),
    ("AVAV", "2026-09-03", "08:13", "Moderate", 7.0),
]


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitized(value, key):
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            if name.casefold() in {"apikey", "api_key", "authorization", "token"}:
                result[name] = "[REDACTED]"
            elif name in {"text", "content", "description"} and isinstance(item, str):
                result[name + "_characters"] = len(item)
                result[name + "_sha256"] = hashlib.sha256(item.encode()).hexdigest()
                result[name + "_excerpt"] = item[:600].replace(key, "[REDACTED]")
            else:
                result[name] = sanitized(item, key)
        return result
    if isinstance(value, list):
        return [sanitized(item, key) for item in value]
    return value.replace(key, "[REDACTED]") if isinstance(value, str) else value


def rows_of(payload):
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("historical"), list):
        return payload["historical"]
    return []


def chart_summary(rows):
    sessions = defaultdict(lambda: {
        "premarket_bars": 0, "regular_bars": 0, "aftermarket_bars": 0,
        "premarket_volume": 0, "regular_volume": 0, "aftermarket_volume": 0,
        "pm_before_0813_volume": 0, "pm_before_0815_volume": 0,
    })
    timestamps = []
    invalid = 0
    for row in rows:
        try:
            stamp = datetime.fromisoformat(row["date"])
            volume = float(row["volume"])
            if volume < 0:
                invalid += 1
                continue
        except (KeyError, ValueError, TypeError):
            invalid += 1
            continue
        timestamps.append(stamp.isoformat(sep=" "))
        target = sessions[stamp.date().isoformat()]
        clock = stamp.time()
        if wall_time(4) <= clock < wall_time(9, 30):
            prefix = "premarket"
            for cutoff in ("0813", "0815"):
                if clock < wall_time(int(cutoff[:2]), int(cutoff[2:])):
                    target["pm_before_" + cutoff + "_volume"] += volume
        elif wall_time(9, 30) <= clock < wall_time(16):
            prefix = "regular"
        elif wall_time(16) <= clock < wall_time(20):
            prefix = "aftermarket"
        else:
            prefix = "other"
        target[prefix + "_bars"] = target.get(prefix + "_bars", 0) + 1
        target[prefix + "_volume"] = target.get(prefix + "_volume", 0) + volume
    return {
        "timezone_assumption": "FMP date interpreted as America/New_York, per existing adapter; no offset in rows",
        "timestamps_are_bar_starts_assumption": True,
        "first": min(timestamps) if timestamps else None,
        "last": max(timestamps) if timestamps else None,
        "duplicate_timestamp_count": len(timestamps) - len(set(timestamps)),
        "invalid_timestamp_or_volume_rows": invalid,
        "sessions": dict(sorted(sessions.items())),
    }


def probe(output, name, endpoint, params):
    target = output / (name + ".json")
    if target.exists():
        print("CACHED " + name, flush=True)
        return json.loads(target.read_text())
    key = os.environ.get("FMP_API_KEY", "").strip()
    if not key:
        raise RuntimeError("FMP_API_KEY must be configured; value is never logged")
    url = "https://financialmodelingprep.com/stable" + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    record = {"name": name, "endpoint": endpoint, "params": params,
              "requested_at_utc": utcnow(), "authentication": "apikey header; omitted"}
    started = time.monotonic()
    payload = None
    try:
        request = urllib.request.Request(url, headers={"apikey": key, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=25) as response:
            raw = response.read(20_000_001)
            record.update(http_status=response.status, server_date=response.headers.get("Date"),
                          content_type=response.headers.get("Content-Type"), response_bytes=len(raw))
            if len(raw) > 20_000_000:
                raise ValueError("response exceeds audit size bound")
            payload = json.loads(raw)
            record["status"] = "OK" if payload else "EMPTY"
            if isinstance(payload, dict) and ("Error Message" in payload or "error" in payload):
                record["status"] = "PROVIDER_ERROR"
    except urllib.error.HTTPError as exc:
        record.update(status="HTTP_ERROR", http_status=exc.code,
                      error_body=exc.read(2000).decode("utf-8", errors="replace"))
    except Exception as exc:
        record.update(status="TRANSPORT_OR_PARSE_ERROR", error_type=type(exc).__name__,
                      error_message=str(exc))
    record.update(elapsed_seconds=round(time.monotonic() - started, 4), received_at_utc=utcnow())
    rows = rows_of(payload)
    record["row_count"] = len(rows)
    record["fields"] = sorted({field for row in rows for field in row})
    record["null_counts"] = {field: sum(row.get(field) is None for row in rows)
                             for field in record["fields"]}
    if endpoint.startswith("/historical-chart/"):
        record["chart"] = chart_summary(rows)
    record["payload"] = sanitized(payload, key)
    encoded = json.dumps(sanitized(record, key), indent=2, ensure_ascii=False, allow_nan=False)
    if key in encoded:
        raise RuntimeError("secret redaction failed")
    target.write_text(encoded + "\n", encoding="utf-8")
    print(json.dumps({"name": name, "status": record["status"],
                      "http": record.get("http_status"), "rows": len(rows),
                      "seconds": record["elapsed_seconds"]}), flush=True)
    if record.get("http_status") == 429:
        raise RuntimeError("rate limit encountered; audit stops without retry")
    time.sleep(0.25)
    return record


def core_jobs():
    symbols = ",".join(row[0] for row in CASES)
    return [
        ("batch_quote", "/batch-quote", {"symbols": symbols}),
        ("aftermarket_trade", "/batch-aftermarket-trade", {"symbols": symbols}),
        ("aftermarket_quote", "/batch-aftermarket-quote", {"symbols": symbols}),
        ("earnings_calendar", "/earnings-calendar", {"from": "2026-08-24", "to": "2026-09-10"}),
        ("earnings_gtlb", "/earnings", {"symbol": "GTLB", "limit": 20}),
        ("estimates_gtlb", "/analyst-estimates", {"symbol": "GTLB", "period": "quarter", "limit": 20}),
        ("news_latest", "/news/stock-latest", {"page": 0, "limit": 30}),
        ("news_gtlb", "/news/stock", {"symbols": "GTLB", "from": "2026-08-31", "to": "2026-09-03", "limit": 50}),
        ("releases_latest", "/news/press-releases-latest", {"page": 0, "limit": 20}),
        ("releases_gtlb", "/news/press-releases", {"symbols": "GTLB", "from": "2026-08-31", "to": "2026-09-03", "limit": 20}),
        ("transcript_gtlb", "/earning-call-transcript", {"symbol": "GTLB", "year": 2027, "quarter": 2}),
        ("income_gtlb", "/income-statement", {"symbol": "GTLB", "period": "quarter", "limit": 4}),
        ("cashflow_afrm", "/cash-flow-statement", {"symbol": "AFRM", "period": "quarter", "limit": 4}),
        ("profile_ntsk", "/profile", {"symbol": "NTSK"}),
        ("profile_psnyw", "/profile", {"symbol": "PSNYW"}),
        ("market_hours", "/exchange-market-hours", {"exchange": "NASDAQ"}),
        ("minute_veev", "/historical-chart/1min", {"symbol": "VEEV", "from": "2026-08-27", "to": "2026-08-27"}),
        ("minute_snow", "/historical-chart/1min", {"symbol": "SNOW", "from": "2026-09-03", "to": "2026-09-03"}),
        ("five_minute_snow", "/historical-chart/5min", {"symbol": "SNOW", "from": "2026-09-03", "to": "2026-09-03"}),
    ]


def case_jobs():
    jobs = []
    for symbol, session, _, _, _ in CASES:
        start = (date.fromisoformat(session) - timedelta(days=4)).isoformat()
        jobs.extend([
            ("case_news_" + symbol, "/news/stock", {"symbols": symbol, "from": start, "to": session, "limit": 100}),
            ("case_minute_" + symbol, "/historical-chart/1min", {"symbol": symbol, "from": session, "to": session}),
            ("case_daily_" + symbol, "/historical-price-eod/full", {"symbol": symbol, "from": start, "to": session}),
        ])
    return jobs


def history_jobs():
    return [
        ("history_" + symbol, "/historical-chart/1min", {"symbol": symbol, "from": "2026-07-27", "to": "2026-09-03"})
        for symbol in ("SNOW", "VEEV", "NYAX")
    ]


def supplemental_jobs():
    sessions = ("2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27",
                "2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03")
    jobs = [("calendar_" + session, "/earnings-calendar", {"from": session, "to": session})
            for session in sessions]
    for symbol, session, _, _, _ in CASES:
        jobs.append(("case_release_" + symbol, "/news/press-releases", {
            "symbols": symbol,
            "from": (date.fromisoformat(session) - timedelta(days=4)).isoformat(),
            "to": session, "limit": 50,
        }))
    for symbol, session in (("EOSE", "2026-09-02"), ("SOLS", "2026-08-28"),
                            ("NVDA", "2026-08-27"), ("CRM", "2026-08-27")):
        jobs.append(("context_news_" + symbol, "/news/stock", {
            "symbols": symbol, "from": (date.fromisoformat(session) - timedelta(days=2)).isoformat(),
            "to": session, "limit": 100,
        }))
    return jobs


def followup_jobs():
    jobs = []
    for symbol, session in (("SNOW", "2026-07-27"), ("VEEV", "2026-08-06"),
                            ("NYAX", "2026-08-20"), ("SNOW", "2026-09-04"),
                            ("NYAX", "2026-09-04")):
        jobs.append(("single_day_" + symbol + "_" + session, "/historical-chart/1min",
                     {"symbol": symbol, "from": session, "to": session}))
    jobs.append(("context_news_NVDA_page1", "/news/stock", {
        "symbols": "NVDA", "from": "2026-08-25", "to": "2026-08-27", "limit": 100, "page": 1,
    }))
    return jobs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("core", "cases", "history", "supplemental", "followup"), required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "evidence")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    jobs = {"core": core_jobs, "cases": case_jobs, "history": history_jobs,
            "supplemental": supplemental_jobs, "followup": followup_jobs}[args.phase]()
    records = [probe(args.output, *job) for job in jobs]
    summary = {"completed_at_utc": utcnow(), "phase": args.phase,
               "requests": len(records), "statuses": dict(Counter(row["status"] for row in records)),
               "median_seconds": statistics.median(row["elapsed_seconds"] for row in records),
               "cases_are_user_selected_not_scanner_output": True,
               "screenshots_timezone_assumption": "Asia/Shanghai; displayed times converted to America/New_York",
               "records": [{key: value for key, value in row.items() if key not in {"payload", "null_counts"}}
                           for row in records]}
    (args.output / (args.phase + "_summary.json")).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key != "records"}), flush=True)


if __name__ == "__main__":
    main()
