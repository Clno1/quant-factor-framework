from __future__ import annotations

import json
from pathlib import Path
import unittest

import pandas as pd

from src.group_analytics.aggregation import aggregate_groups
from src.group_analytics.returns import compute_eod_returns


FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "group_analytics"
    / "golden_10_sessions.json"
)


class TenSessionGoldenReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_ten_sessions_reconcile_returns_statistics_breadth_and_contributions(self):
        fixture = self.fixture
        sessions = pd.to_datetime(fixture["sessions"])
        multipliers = fixture["session_multipliers"]
        self.assertEqual(len(sessions), 11)
        self.assertEqual(len(multipliers), 10)

        closes: dict[str, list[float]] = {}
        ticker_group: dict[str, dict] = {}
        for group in fixture["groups"]:
            for ticker, base_return in zip(group["tickers"], group["base_returns"]):
                path = [100.0]
                for multiplier in multipliers:
                    path.append(path[-1] * (1.0 + base_return * multiplier))
                closes[ticker] = path
                ticker_group[ticker] = group
        adjusted_close = pd.DataFrame(closes, index=sessions)

        for position, (asof, multiplier) in enumerate(
            zip(sessions[1:], multipliers), start=1
        ):
            with self.subTest(asof=asof.date().isoformat()):
                returns = compute_eod_returns(
                    adjusted_close.iloc[position - 1 : position + 1],
                    asof=asof,
                )
                rows = []
                for ticker, value in returns.items():
                    group = ticker_group[ticker]
                    rows.append(
                        {
                            "group_id": group["group_id"],
                            "group_name": group["group_name"],
                            "level": "sector",
                            "security_id": f"fixture:{ticker}",
                            "counting_unit_id": f"security:{ticker}",
                            "ticker": ticker,
                            "name": ticker,
                            "raw_return_1d": float(value),
                            "reason_codes": [],
                        }
                    )

                result = aggregate_groups(pd.DataFrame(rows), benchmark_return_1d=0.0)
                metrics = result.metrics.set_index("group_id")
                contributions = result.contributions
                for group in fixture["groups"]:
                    metric = metrics.loc[group["group_id"]]
                    expected = group["expected"]
                    self.assertEqual(int(metric["n_expected"]), 5)
                    self.assertEqual(int(metric["n_valid"]), 5)
                    self.assertAlmostEqual(float(metric["count_coverage"]), 1.0)
                    for field in (
                        "raw_ew_return_1d",
                        "median_return_1d",
                        "robust_ew_return_1d",
                    ):
                        self.assertAlmostEqual(
                            float(metric[field]),
                            expected[f"{field}_per_multiplier"] * multiplier,
                            places=10,
                        )
                    sign = "positive" if multiplier > 0 else "negative"
                    self.assertAlmostEqual(
                        float(metric["up_pct"]), expected[f"{sign}_up_pct"]
                    )
                    self.assertAlmostEqual(
                        float(metric["down_pct"]), expected[f"{sign}_down_pct"]
                    )
                    self.assertAlmostEqual(
                        float(metric["breadth_net"]),
                        expected[f"{sign}_breadth_net"],
                    )
                    group_contributions = contributions[
                        contributions["group_id"].eq(group["group_id"])
                    ]
                    self.assertEqual(len(group_contributions), 5)
                    self.assertAlmostEqual(
                        float(group_contributions["contribution"].sum()),
                        float(metric["robust_ew_return_1d"]),
                        places=12,
                    )


if __name__ == "__main__":
    unittest.main()
