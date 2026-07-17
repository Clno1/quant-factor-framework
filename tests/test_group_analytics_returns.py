from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from src.group_analytics.calendar import latest_completed_session, official_session_close
from src.group_analytics.returns import compute_eod_return_audit, compute_eod_returns


class _FakeXNYSCalendar:
    def sessions_in_range(self, _start, _end):
        return pd.DatetimeIndex(["2026-07-14", "2026-07-15"])

    def session_close(self, session):
        label = pd.Timestamp(session).tz_localize(None).normalize()
        # July closes at 16:00 America/New_York == 20:00 UTC.
        return label.tz_localize("UTC") + pd.Timedelta(hours=20)


class GroupAnalyticsReturnTests(unittest.TestCase):
    def test_adjusted_close_return_matches_manual(self):
        prices = pd.DataFrame(
            {"A": [100.0, 110.0], "B": [50.0, 45.0]},
            index=pd.to_datetime(["2026-07-14", "2026-07-15"]),
        )

        returns = compute_eod_returns(prices, asof="2026-07-15")

        self.assertAlmostEqual(returns["A"], 0.10)
        self.assertAlmostEqual(returns["B"], -0.10)
        self.assertEqual(returns.attrs["return_status"], "FINAL")

    def test_pct_change_does_not_fill_price_gap(self):
        prices = pd.DataFrame(
            {"A": [100.0, np.nan, 110.0, 121.0]},
            index=pd.to_datetime(
                ["2026-07-10", "2026-07-13", "2026-07-14", "2026-07-15"]
            ),
        )

        missing = compute_eod_returns(prices, asof="2026-07-14")
        valid = compute_eod_returns(prices, asof="2026-07-15")

        self.assertTrue(np.isnan(missing["A"]))
        self.assertAlmostEqual(valid["A"], 0.10)

    def test_missing_and_non_positive_prices_are_not_zero_returns(self):
        prices = pd.DataFrame(
            {
                "MISSING_NOW": [100.0, np.nan],
                "MISSING_PREV": [np.nan, 100.0],
                "ZERO": [100.0, 0.0],
            },
            index=pd.to_datetime(["2026-07-14", "2026-07-15"]),
        )

        audit = compute_eod_return_audit(prices, asof="2026-07-15")

        self.assertTrue(audit["raw_return_1d"].isna().all())
        self.assertIn("MISSING_RETURN", audit.loc["MISSING_NOW", "reason_codes"])
        self.assertIn("MISSING_PREVIOUS_CLOSE", audit.loc["MISSING_PREV", "reason_codes"])

    def test_split_uses_adjusted_not_raw_close(self):
        # Raw price appears to halve on a 2:1 split, while adjusted close is continuous.
        adjusted = pd.DataFrame(
            {"SPLIT": [50.0, 51.0]},
            index=pd.to_datetime(["2026-07-14", "2026-07-15"]),
        )

        result = compute_eod_returns(adjusted, asof="2026-07-15")

        self.assertAlmostEqual(result["SPLIT"], 0.02)

    def test_missing_delisting_return_is_flagged(self):
        prices = pd.DataFrame(
            {"DELISTED": [25.0, np.nan], "ORDINARY": [10.0, np.nan]},
            index=pd.to_datetime(["2026-07-14", "2026-07-15"]),
        )

        audit = compute_eod_return_audit(
            prices,
            asof="2026-07-15",
            delisting_return_required={"DELISTED": True},
        )

        self.assertFalse(bool(audit.loc["DELISTED", "is_valid_return"]))
        self.assertIn(
            "MISSING_DELISTING_RETURN",
            audit.loc["DELISTED", "reason_codes"],
        )
        self.assertNotIn(
            "MISSING_DELISTING_RETURN",
            audit.loc["ORDINARY", "reason_codes"],
        )


class ExchangeCalendarTests(unittest.TestCase):
    def test_latest_session_waits_for_official_close(self):
        calendar = _FakeXNYSCalendar()

        before = latest_completed_session(
            now=pd.Timestamp("2026-07-15T19:59:59Z"), calendar=calendar
        )
        after = latest_completed_session(
            now=pd.Timestamp("2026-07-15T20:00:00Z"), calendar=calendar
        )

        self.assertEqual(before, pd.Timestamp("2026-07-14"))
        self.assertEqual(after, pd.Timestamp("2026-07-15"))
        self.assertEqual(
            official_session_close("2026-07-15", calendar=calendar),
            pd.Timestamp("2026-07-15T20:00:00Z"),
        )

    def test_real_xnys_calendar_handles_holiday_early_close_and_dst(self):
        try:
            import exchange_calendars as xcals
        except ImportError:
            self.skipTest("exchange-calendars is installed from requirements.txt in deployment")

        from src.group_analytics.calendar import previous_session

        calendar = xcals.get_calendar("XNYS")
        self.assertFalse(calendar.is_session("2026-07-03"))
        self.assertEqual(
            previous_session("2026-07-06", calendar=calendar).date().isoformat(),
            "2026-07-02",
        )
        # DST moves a normal NYSE close from 21:00 UTC to 20:00 UTC.
        self.assertEqual(
            official_session_close("2026-03-06", calendar=calendar).hour, 21
        )
        self.assertEqual(
            official_session_close("2026-03-09", calendar=calendar).hour, 20
        )
        # The session after US Thanksgiving is an official 13:00 ET close.
        self.assertEqual(
            official_session_close("2026-11-27", calendar=calendar).hour, 18
        )
        self.assertEqual(
            latest_completed_session(
                now="2026-11-27T17:59:00Z", calendar=calendar
            ).date().isoformat(),
            "2026-11-25",
        )
        self.assertEqual(
            latest_completed_session(
                now="2026-11-27T18:01:00Z", calendar=calendar
            ).date().isoformat(),
            "2026-11-27",
        )


if __name__ == "__main__":
    unittest.main()
