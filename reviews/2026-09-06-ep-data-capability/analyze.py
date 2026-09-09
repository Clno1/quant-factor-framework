#!/usr/bin/env python3
"""Derive case evidence from saved probes, without any network access."""
from collections import Counter
from datetime import datetime, timedelta
import json
from pathlib import Path
import statistics

from probe import CASES


ROOT = Path(__file__).parent
EVIDENCE = ROOT / "evidence"


def read(name):
    return json.loads((EVIDENCE / (name + ".json")).read_text())


def before(rows, cutoff):
    accepted = []
    for row in rows:
        try:
            published = datetime.fromisoformat(row["publishedDate"])
        except (KeyError, TypeError, ValueError):
            continue
        if published <= cutoff:
            accepted.append(row)
    return accepted


def is_catalyst_headline(symbol, title):
    text = title.casefold()
    if symbol == "NYAX":
        return "acquire ips" in text
    if symbol == "AVAV":
        return "locust" in text and "contract" in text
    return "results" in text and ("quarter" in text or "fiscal" in text)


def main():
    calendar = []
    calendar_checks = []
    for path in sorted(EVIDENCE.glob("calendar_*.json")):
        record = json.loads(path.read_text())
        rows = record.get("payload") or []
        calendar.extend(rows)
        calendar_checks.append({"requested_date": record["params"]["from"], "rows": len(rows),
                                "returned_dates": sorted({row["date"] for row in rows})})
    cases = []
    for symbol, session, at, grade, screenshot_change in CASES:
        cutoff = datetime.fromisoformat(session + " " + at)
        news = read("case_news_" + symbol)
        releases = read("case_release_" + symbol)
        minute = read("case_minute_" + symbol)
        daily = read("case_daily_" + symbol)
        visible_releases = before(releases.get("payload") or [], cutoff)
        matching_releases = [row for row in visible_releases if is_catalyst_headline(symbol, row["title"])]
        bars = minute.get("payload") or []
        buckets = Counter(row["date"][:14] + str(int(row["date"][14:16]) // 5) for row in bars)
        observed = sorted(datetime.fromisoformat(row["date"]) for row in bars)
        start = datetime.fromisoformat(session + " 09:30:00")
        regular_expected = {start + timedelta(minutes=i) for i in range(390)}
        daily_rows = {row["date"][:10]: row for row in daily.get("payload") or []}
        previous_days = sorted(day for day in daily_rows if day < session)
        reference = daily_rows[previous_days[-1]]["close"] if previous_days else None
        event_daily = daily_rows.get(session) or {}
        relevant_financials = [row for row in calendar if row["symbol"] == symbol
                              and session >= row["date"] >= (cutoff - timedelta(days=4)).date().isoformat()]
        case = {
            "ticker": symbol, "session": session, "screenshot_cutoff_et_assumed": cutoff.isoformat(),
            "screenshot_grade_unverified": grade, "screenshot_change_pct_unverified": screenshot_change,
            "screenshot_is_post_open": at >= "09:30",
            "news_rows": news["row_count"],
            "news_rows_published_before_cutoff": len(before(news.get("payload") or [], cutoff)),
            "release_rows": releases["row_count"],
            "matching_catalyst_releases_before_cutoff": [
                {key: row.get(key) for key in ("publishedDate", "title", "url", "text_characters")}
                for row in matching_releases
            ],
            "provider_first_seen_at_historical": None,
            "historical_news_arrival_latency_verified": False,
            "calendar_financials_current_snapshot": relevant_financials,
            "minute_rows": len(bars), "complete_five_minute_buckets": sum(n == 5 for n in buckets.values()),
            "missing_regular_minute_labels_unclassified": sorted(t.isoformat() for t in regular_expected - set(observed)),
            "chart": minute["chart"], "premarket_volume_status": "NOT_PROVIDED_BY_TESTED_BAR_ENDPOINT",
            "daily_previous_close": reference,
            "daily_event_open": event_daily.get("open"),
            "event_open_gap_pct_not_premarket": (event_daily["open"] / reference - 1) * 100
                if reference and event_daily.get("open") else None,
            "event_daily_volume": event_daily.get("volume"),
            "event_regular_minute_volume": sum(float(row["volume"]) for row in bars),
            "regular_minute_to_eod_volume_ratio_not_coverage":
                sum(float(row["volume"]) for row in bars) / event_daily["volume"]
                if event_daily.get("volume") else None,
            "full_market_discovery_tested": False,
        }
        cases.append(case)
    probes = []
    for path in EVIDENCE.glob("*.json"):
        record = json.loads(path.read_text())
        if "endpoint" in record:
            probes.append(record)
    latency = sorted(row["elapsed_seconds"] for row in probes)
    summary = {
        "fmp_archived_requests": len(probes), "fmp_statuses": dict(Counter(row["status"] for row in probes)),
        "http_latency_median_seconds": statistics.median(latency),
        "http_latency_p95_seconds_nearest_rank": latency[max(0, int(len(latency)*0.95 + 0.999999) - 1)],
        "http_latency_max_seconds": max(latency),
        "case_count": len(cases),
        "cases_with_catalyst_release_before_cutoff": sum(bool(row["matching_catalyst_releases_before_cutoff"]) for row in cases),
        "cases_with_regular_bars": sum(bool(row["minute_rows"]) for row in cases),
        "cases_with_extended_bars": sum(any(s["premarket_bars"] + s["aftermarket_bars"] for s in row["chart"]["sessions"].values()) for row in cases),
        "calendar_daily_checks": calendar_checks,
        "all_calendar_split_rows": len(calendar),
        "note": "Coverage is for supplied cases only; no scanner recall, live latency, or historical first-seen proof.",
        "cases": cases,
    }
    target = ROOT / "case_evidence.json"
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in summary.items() if key not in {"cases", "calendar_daily_checks"}}, ensure_ascii=False))
    for row in cases:
        print(json.dumps({key: row[key] for key in ("ticker", "news_rows_published_before_cutoff", "minute_rows", "complete_five_minute_buckets", "calendar_financials_current_snapshot", "matching_catalyst_releases_before_cutoff")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
