#!/usr/bin/env python3
"""Offline checks for this fixed audit snapshot, not strategy acceptance tests."""
from collections import Counter
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import unittest

from analyze import before
from probe import CASES, chart_summary, sanitized


ROOT = Path(__file__).parent
DOCS = ROOT.parents[1] / "docs"


class AuditChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (ROOT / "case_evidence.json").is_file() or not (ROOT / "evidence").is_dir():
            raise unittest.SkipTest("Local vendor audit snapshot is not distributed with source code")
        cls.summary = json.loads((ROOT / "case_evidence.json").read_text())
        cls.cases = cls.summary["cases"]

    def test_archived_request_inventory(self):
        records = [json.loads(path.read_text()) for path in (ROOT / "evidence").glob("*.json")]
        fmp = [row for row in records if "endpoint" in row]
        self.assertEqual(len(fmp), 109)
        self.assertEqual(Counter(row["status"] for row in fmp), {"OK": 109})
        self.assertEqual(self.summary["fmp_archived_requests"], len(fmp))

    def test_known_cases_are_not_claimed_as_discovery(self):
        self.assertEqual({row["ticker"] for row in self.cases}, {row[0] for row in CASES})
        self.assertEqual(len(self.cases), 17)
        for row in self.cases:
            self.assertTrue(row["matching_catalyst_releases_before_cutoff"])
            self.assertFalse(row["full_market_discovery_tested"])
            self.assertFalse(row["historical_news_arrival_latency_verified"])
            self.assertIsNone(row["provider_first_seen_at_historical"])
            self.assertEqual(row["premarket_volume_status"], "NOT_PROVIDED_BY_TESTED_BAR_ENDPOINT")
        self.assertEqual(sum(bool(row["calendar_financials_current_snapshot"]) for row in self.cases), 15)

    def test_news_cutoff_excludes_later_and_malformed_rows(self):
        cutoff = datetime(2026, 9, 2, 8, 13)
        rows = [{"publishedDate": "2026-09-02 08:12:59"},
                {"publishedDate": "2026-09-02 08:13:00"},
                {"publishedDate": "2026-09-02 08:13:01"},
                {"publishedDate": "bad"}, {}]
        self.assertEqual(before(rows, cutoff), rows[:2])

    def test_chart_session_boundaries(self):
        times = ["03:59", "04:00", "08:12", "08:13", "08:15", "09:29",
                 "09:30", "15:59", "16:00", "19:59", "20:00"]
        rows = [{"date": "2026-09-03 " + time + ":00", "volume": 1} for time in times]
        rows += [{"date": "bad", "volume": 1}, {"date": "2026-09-03 08:00:00", "volume": -1}]
        result = chart_summary(rows)
        session = result["sessions"]["2026-09-03"]
        self.assertEqual([session[key + "_bars"] for key in ("premarket", "regular", "aftermarket", "other")], [5, 2, 2, 2])
        self.assertEqual(session["pm_before_0813_volume"], 2)
        self.assertEqual(session["pm_before_0815_volume"], 3)
        self.assertEqual(result["invalid_timestamp_or_volume_rows"], 2)

    def test_minute_gaps_and_volume_ratios_are_reproducible(self):
        gaps = {}
        for case in self.cases:
            bars = json.loads((ROOT / "evidence" / ("case_minute_" + case["ticker"] + ".json")).read_text())["payload"]
            stamps = [datetime.fromisoformat(row["date"]) for row in bars]
            self.assertEqual(len(stamps), len(set(stamps)))
            start = datetime.fromisoformat(case["session"] + " 09:30:00")
            expected = {start + timedelta(minutes=i) for i in range(390)}
            self.assertTrue(set(stamps) <= expected)
            missing = sorted(stamp.isoformat() for stamp in expected - set(stamps))
            self.assertEqual(case["missing_regular_minute_labels_unclassified"], missing)
            if missing:
                gaps[case["ticker"]] = len(missing)
            volumes = [float(row["volume"]) for row in bars]
            self.assertTrue(all(math.isfinite(value) and value >= 0 for value in volumes))
            ratio = sum(volumes) / case["event_daily_volume"]
            self.assertAlmostEqual(ratio, case["regular_minute_to_eod_volume_ratio_not_coverage"])
        self.assertEqual(gaps, {"NYAX": 242, "GTLB": 2, "AGX": 1})

    def test_redaction_and_truncation(self):
        key = "FAKE_SECRET_FOR_TEST_ONLY"
        raw = {"apikey": key, "nested": [{"url": "https://example.test/?apikey=" + key}],
               "text": key + "x" * 1000}
        redacted = sanitized(raw, key)
        self.assertNotIn(key, json.dumps(redacted))
        self.assertEqual(redacted["text_characters"], len(raw["text"]))
        self.assertLess(len(redacted["text_excerpt"]), 601)

    def test_reports_and_local_links(self):
        paths = [DOCS / (name + "_20260906.md") for name in
                 ("ep_data_capability_audit", "ep_case_acceptance_matrix", "ep_v1_scope_acceptance")]
        for path in paths:
            content = path.read_text()
            self.assertTrue(content.startswith("# "))
            for link in re.findall(r"\]\(([^)]+)\)", content):
                if "://" not in link and not link.startswith("#"):
                    self.assertTrue((path.parent / link.split("#")[0]).exists(), (path.name, link))
            self.assertTrue(all(line == line.rstrip() for line in content.splitlines()), path.name)

    def test_no_live_key_in_new_artifacts(self):
        key = os.environ.get("FMP_API_KEY", "").strip()
        if not key:
            self.skipTest("No environment key available for leak check")
        paths = list(ROOT.rglob("*.json")) + list(ROOT.glob("*.py")) + list(DOCS.glob("ep_*_20260906.md"))
        for path in paths:
            self.assertFalse(key in path.read_text(), "Secret leak in " + path.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
