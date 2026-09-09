from __future__ import annotations

import ast
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.breakouts.ep.classifier import classify
from src.breakouts.ep.models import EpSettings, canonical_url, normalize_evidence
from src.breakouts.ep.service import EpRadar
from src.breakouts.ep.store import EpStore


NOW = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def article(symbol="SNOW", **changes):
    return {"symbol": symbol, "title": f"{symbol} Reports Second Quarter Fiscal 2027 Results",
            "publishedDate": "2026-09-02 16:05:00", "text": "Revenue and earnings were reported.",
            "url": f"https://example.test/{symbol}/results", **changes}


def financials(symbol="SNOW", **changes):
    return {"symbol": symbol, "date": "2026-09-02", "epsActual": .62,
            "epsEstimated": .4468, "revenueActual": 1546793000,
            "revenueEstimated": 1483224000, "lastUpdated": "2026-09-03", **changes}


class FakeProvider:
    def __init__(self, articles=None, calendar=None, profiles=None):
        self.news = articles if articles is not None else [article()]
        self.days = calendar if calendar is not None else [financials()]
        self.identities = profiles or {}
        self.calls = []

    def articles(self, feed, page, limit, *, timeout):
        self.calls.append((feed, page, timeout))
        return self.news[page * limit:(page + 1) * limit] if feed == "press" else []

    def calendar(self, day, *, timeout):
        self.calls.append(("calendar", day, timeout))
        return [row for row in self.days if row["date"] == day]

    def profile(self, symbol, *, timeout):
        self.calls.append(("profile", symbol, timeout))
        return self.identities.get(symbol, {"ticker": symbol, "name": symbol, "asset_type": "STOCK",
            "exchange": "NASDAQ", "is_actively_trading": True})


class EvidenceTests(unittest.TestCase):
    def test_url_dedup_keeps_semantic_query(self):
        self.assertEqual(canonical_url("https://EXAMPLE.test/story/?utm_source=a#top"), "https://example.test/story")
        self.assertNotEqual(canonical_url("https://example.test/?id=1"), canonical_url("https://example.test/?id=2"))
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "https://u:p@example.test/a"):
            with self.assertRaises(ValueError):
                canonical_url(bad)

    def test_identity_stable_revision_changes(self):
        original = normalize_evidence("ARTICLE", article(), NOW)
        duplicate = normalize_evidence("ARTICLE", article(url=article()["url"] + "/?utm_source=x"), NOW)
        updated = normalize_evidence("ARTICLE", article(text="Corrected earnings"), NOW)
        self.assertEqual(original.document_id, duplicate.document_id)
        self.assertEqual(original.revision_id, duplicate.revision_id)
        self.assertEqual(original.document_id, updated.document_id)
        self.assertNotEqual(original.revision_id, updated.revision_id)

    def test_times_and_invalid_financial_numbers(self):
        with self.assertRaises(ValueError):
            normalize_evidence("ARTICLE", article(), NOW.replace(tzinfo=None))
        with self.assertRaises(ValueError):
            normalize_evidence("ARTICLE", article(publishedDate="2026-09-04 10:00:00"), NOW)
        with self.assertRaises(ValueError):
            normalize_evidence("CALENDAR", financials(epsActual=float("nan")), NOW)
        with self.assertRaises(ValueError):
            normalize_evidence("CALENDAR", financials(epsActual=True), NOW)
        evidence = normalize_evidence("ARTICLE", article(), NOW)
        self.assertEqual(evidence.published_at, "2026-09-02T20:05:00.000000+00:00")
        self.assertTrue(evidence.payload["timezone_assumed"])
        calendar = normalize_evidence("CALENDAR", financials(), NOW)
        self.assertIsNone(calendar.published_at)

    def test_legal_notice_is_not_earnings(self):
        snap = normalize_evidence("ARTICLE", article(title="Shareholder Alert: Class Action after Fiscal Results"), NOW)
        doc = {**snap.to_dict(), "first_seen_at": snap.observed_at}
        self.assertEqual(classify(doc)["event_type_hint"], "LEGAL_NOTICE")
        self.assertEqual(classify(doc)["relation"], "UNVERIFIED")


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ep.sqlite3"
        self.store = EpStore(self.path)

    def collect(self, provider=None, now=NOW, **settings):
        return EpRadar(self.store, provider or FakeProvider(), EpSettings(**settings), clock=lambda: now).collect(
            "2026-09-02", "2026-09-03")

    def test_end_to_end_report_and_explain_are_shadow(self):
        result = self.collect()
        self.assertEqual(result["status"], "COMPLETE_OBSERVATION")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["ticker"], "SNOW")
        self.assertEqual(candidate["status"], "EVIDENCE_PENDING")
        self.assertEqual(candidate["delivery"], "DISABLED_SHADOW_ONLY")
        self.assertIsNone(candidate["grade"])
        self.assertIsNone(candidate["premarket"]["volume"])
        self.assertIsNone(candidate["financials"][0]["surprise_pct"])
        self.assertFalse(result["summary"]["market_coverage_proven"])
        self.assertEqual(self.store.explain("SNOW")["reason"], "EVALUATED")
        self.assertEqual(self.store.explain("COIN")["reason"], "NOT_FOUND_IN_FETCHED_SCOPE")

    def test_observed_time_blocks_backfilled_history(self):
        self.collect()
        self.assertEqual(self.store.documents("2026-09-02", "2026-09-03", NOW - timedelta(seconds=1)), [])
        self.assertEqual(self.store.explain("SNOW", as_of=NOW - timedelta(seconds=1))["reason"], "NO_RUN_AVAILABLE_AS_OF")
        self.assertEqual(len(self.store.documents("2026-09-02", "2026-09-03", NOW)), 2)

    def test_revision_reversion_and_idempotent_repeats(self):
        self.collect()
        self.collect(FakeProvider(articles=[article(text="Corrected")]), NOW + timedelta(minutes=1))
        self.collect(now=NOW + timedelta(minutes=2))
        def body(at):
            return next(doc["payload"]["text"] for doc in self.store.documents("2026-09-02", "2026-09-03", at)
                        if doc["kind"] == "ARTICLE")
        self.assertEqual(body(NOW), article()["text"])
        self.assertEqual(body(NOW + timedelta(minutes=1)), "Corrected")
        self.assertEqual(body(NOW + timedelta(minutes=2)), article()["text"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ep_documents").fetchone()[0], 3)

    def test_etfs_and_warrants_are_not_ordinary_stocks(self):
        profiles = {symbol: {"ticker": symbol, "asset_type": kind, "exchange": "NASDAQ", "is_actively_trading": True}
                    for symbol, kind in (("SOXL", "ETF"), ("PSNYW", "WARRANT"))}
        provider = FakeProvider([article("SOXL"), article("PSNYW")], [], profiles)
        result = self.collect(provider)
        self.assertTrue(all(row["status"] == "EXCLUDED" for row in result["candidates"]))
        result = self.collect(provider, NOW + timedelta(minutes=1), include_etfs=True)
        statuses = {row["ticker"]: row["status"] for row in result["candidates"]}
        self.assertEqual(statuses, {"PSNYW": "EXCLUDED", "SOXL": "EVIDENCE_PENDING"})

    def test_profiles_defer_without_losing_candidates_and_cache_makes_progress(self):
        provider = FakeProvider([article("AAA"), article("BBB"), article("CCC")], [])
        first = self.collect(provider, max_profiles=1)
        self.assertEqual(first["summary"]["candidate_count"], 3)
        self.assertEqual(first["summary"]["identity_pending_count"], 2)
        second = self.collect(provider, NOW + timedelta(minutes=1), max_profiles=1)
        self.assertEqual(second["summary"]["identity_pending_count"], 1)
        self.assertEqual(second["status"], "PARTIAL")

    def test_caps_repeated_pages_and_invalid_calendar_are_visible(self):
        capped = self.collect(FakeProvider([article("AAA"), article("BBB")], []), page_size=1, max_pages=1)
        self.assertIn("PAGINATION_LIMIT_REACHED", {row["status"] for row in capped["summary"]["coverage"]})
        provider = FakeProvider()
        provider.articles = lambda feed, page, limit, timeout: [article()] if feed == "press" else []
        repeated = self.collect(provider, page_size=1, max_pages=3)
        self.assertIn("REPEATED_PAGE", {row["status"] for row in repeated["summary"]["coverage"]})
        provider = FakeProvider()
        provider.calendar = lambda day, timeout: [financials(date="2026-08-01")]
        invalid = self.collect(provider)
        self.assertIn("INVALID_RECORDS", {row["status"] for row in invalid["summary"]["coverage"]})
        self.assertTrue(any(row["rejected"] for row in invalid["pages"]))

    def test_budgets_and_transport_failure(self):
        result = self.collect(max_requests=1)
        self.assertEqual(result["summary"]["requests"], 1)
        self.assertEqual(result["status"], "PARTIAL")
        provider = FakeProvider()
        provider.calendar = Mock(side_effect=TimeoutError("secret must not be logged"))
        result = self.collect(provider)
        self.assertIn("PROVIDER_ERROR_TimeoutError", json.dumps(result))
        self.assertNotIn("secret must not be logged", json.dumps(result))
        clock = iter([0, 200, 200, 200, 200])
        result = EpRadar(self.store, FakeProvider(), clock=lambda: NOW,
                        monotonic=lambda: next(clock)).collect("2026-09-02", "2026-09-02")
        self.assertEqual(result["summary"]["requests"], 0)
        self.assertIn("TIME_BUDGET_EXCEEDED", json.dumps(result))

    def test_scope_validation_and_read_only_inspection(self):
        with self.assertRaises(ValueError):
            EpRadar(self.store, FakeProvider(), clock=lambda: NOW).collect("2026-08-01", "2026-09-03")
        with self.assertRaises(ValueError):
            EpRadar(self.store, FakeProvider(), clock=lambda: NOW).collect("2026-09-03", "2026-09-04")
        self.collect()
        before = self.path.read_bytes()
        self.assertEqual(EpStore(self.path, read_only=True).explain("SNOW")["reason"], "EVALUATED")
        self.assertEqual(before, self.path.read_bytes())
        missing = Path(self.temp.name) / "missing.sqlite"
        with self.assertRaises(FileNotFoundError):
            EpStore(missing, read_only=True)
        self.assertFalse(missing.exists())
        foreign = Path(self.temp.name) / "foreign.sqlite"
        with sqlite3.connect(foreign) as db:
            db.execute("CREATE TABLE user_data (value)")
        with self.assertRaises(ValueError):
            EpStore(foreign)

    def test_calendar_cap_and_rate_limit_do_not_claim_complete(self):
        provider = FakeProvider()
        provider.calendar = lambda day, timeout: [financials(date=day)] * 4000
        result = self.collect(provider)
        self.assertIn("CALENDAR_LIMIT_REACHED", json.dumps(result["summary"]["coverage"]))
        self.assertEqual(result["status"], "PARTIAL")
        import requests
        response = requests.Response()
        response.status_code = 429
        provider = FakeProvider()
        provider.articles = Mock(side_effect=requests.HTTPError(response=response))
        result = self.collect(provider)
        self.assertEqual(result["summary"]["requests"], 1)
        self.assertIn("PROVIDER_HTTP_429", json.dumps(result["summary"]["coverage"]))

    def test_interrupted_run_and_invalid_timestamps_are_visible(self):
        unfinished = self.store.start_run("2026-09-02", "2026-09-03", {}, NOW)
        self.assertEqual(self.store.explain("SNOW")["reason"], "RUN_NOT_FINISHED")
        result = self.collect(FakeProvider([article(publishedDate="no-time")], []))
        self.assertEqual(self.store.report(unfinished)["status"], "INTERRUPTED")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("PUBLICATION_TIME_MISSING", json.dumps(result["pages"]))

    def test_negative_eps_is_retained_without_misleading_surprise(self):
        result = self.collect(FakeProvider([article("NTSK")], [financials("NTSK", epsActual=-.03, epsEstimated=-.06775)]))
        value = result["candidates"][0]["financials"][0]
        self.assertEqual(value["epsActual"], -.03)
        self.assertIsNone(value["surprise_pct"])

    def test_window_boundary_and_bad_provider_payload(self):
        old = article("OLD", publishedDate="2026-08-20 07:00:00")
        result = self.collect(FakeProvider([article(), old], []), page_size=2)
        self.assertIn("WINDOW_BOUNDARY_REACHED", json.dumps(result["summary"]["coverage"]))
        self.assertEqual([row["ticker"] for row in result["candidates"]], ["SNOW"])
        provider = FakeProvider()
        provider.articles = lambda feed, page, limit, timeout: {"error": "bad"}
        result = self.collect(provider)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("INVALID_PAYLOAD", json.dumps(result["pages"]))

    def test_invalid_row_does_not_block_next_news_page(self):
        provider = FakeProvider([article(symbol=""), article()], [])
        result = self.collect(provider, page_size=1, max_pages=3)
        self.assertEqual([row["ticker"] for row in result["candidates"]], ["SNOW"])
        self.assertEqual(result["status"], "PARTIAL")
        coverage = next(row for row in result["summary"]["coverage"] if row["feed"] == "press")
        self.assertEqual(coverage["pagination_status"], "END_OF_FEED")
        self.assertTrue(any(row["rejected"] and "source_title" in row["rejected"][0] for row in result["pages"]))

    def test_supported_event_hints_receive_identity_budget_first(self):
        provider = FakeProvider([article("AAA", title="What investors watched today"), article("ZZZ")], [])
        result = self.collect(provider, max_profiles=1)
        selected = [row["ticker"] for row in result["candidates"] if row["identity"] is not None]
        self.assertEqual(selected, ["ZZZ"])

    def test_cli_explain_does_not_load_network_adapter(self):
        self.collect()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/run_ep_radar.py"), "--db", str(self.path),
                                 "explain", "SNOW"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["reason"], "EVALUATED")
        stdlib = subprocess.run([sys.executable, "-S", str(ROOT / "scripts/run_ep_radar.py"), "--db", str(self.path),
                                  "explain", "SNOW"], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(stdlib.stdout)["reason"], "EVALUATED")


class IsolationTests(unittest.TestCase):
    def test_legacy_public_exports_still_resolve(self):
        import src.breakouts as package
        from src.breakouts.scanner import BreakoutFilters
        from src.breakouts.intraday import build_intraday_snapshot
        self.assertIs(package.BreakoutFilters, BreakoutFilters)
        self.assertIs(package.build_intraday_snapshot, build_intraday_snapshot)
        self.assertIn("scan_breakouts", dir(package))
        with self.assertRaises(AttributeError):
            getattr(package, "not_an_export")

    def test_ep_does_not_import_web_cup_monitor_or_delivery(self):
        for path in (ROOT / "src/breakouts/ep").glob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = ([node.module or ""] if isinstance(node, ast.ImportFrom) else
                         [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                for name in names:
                    self.assertFalse(any(part in name for part in
                        ("webapp", "breakouts.live", "discord", "requests", "reviews", "urllib.request")), (path.name, name))


class FmpAdapterTests(unittest.TestCase):
    def test_news_calendar_and_profiles_use_shared_adapter(self):
        from src.data import fmp
        response = Mock()
        response.json.return_value = []
        with patch.object(fmp, "_request", return_value=response) as request:
            self.assertEqual(fmp.get_ep_news_page("press", page=2, limit=50), [])
            request.assert_called_with("/news/press-releases-latest", {"page": 2, "limit": 50},
                                       timeout=10, retry=0, rate_limit_passthrough=True)
            fmp.get_ep_earnings_calendar_day("2026-09-02")
            request.assert_called_with("/earnings-calendar", {"from": "2026-09-02", "to": "2026-09-02"},
                                       timeout=10, retry=0, rate_limit_passthrough=True)
            response.json.return_value = {"Error Message": "bad"}
            with self.assertRaises(ValueError):
                fmp.get_ep_news_page("stock")
        row = {"symbol": "PSNYW", "companyName": "Polestar", "exchangeShortName": "NASDAQ",
               "isEtf": False, "isFund": False, "isAdr": False, "isActivelyTrading": True}
        with patch.object(fmp, "_ep_records", return_value=[row]):
            self.assertEqual(fmp.get_ep_security_profile("PSNYW")["asset_type"], "WARRANT")
            with self.assertRaises(ValueError):
                fmp.get_ep_security_profile("COIN")

    def test_ep_rate_limits_propagate_without_retry(self):
        import requests
        from src.data import fmp
        response = requests.Response()
        response.status_code = 429
        with patch.object(fmp, "get_api_key", return_value="test"), \
                patch.object(fmp.requests, "get", return_value=response) as get, patch.object(fmp.time, "sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                fmp.get_ep_news_page("stock")
            self.assertEqual(get.call_count, 1)
            sleep.assert_not_called()

    def test_existing_request_rate_limit_behavior_is_unchanged(self):
        import requests
        from src.data import fmp
        limited, success = requests.Response(), requests.Response()
        limited.status_code, success.status_code = 429, 200
        with patch.object(fmp, "get_api_key", return_value="test"), \
                patch.object(fmp.requests, "get", side_effect=[limited, success]) as get, \
                patch.object(fmp.time, "sleep") as sleep:
            self.assertIs(fmp._request("/profile", retry=1), success)
            self.assertEqual(get.call_count, 2)
            sleep.assert_called_once_with(2.0)


if __name__ == "__main__":
    unittest.main()
