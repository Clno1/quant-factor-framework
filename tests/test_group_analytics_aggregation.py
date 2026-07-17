from __future__ import annotations

import random
import unittest

import numpy as np
import pandas as pd

from src.group_analytics.aggregation import (
    aggregate_group_members,
    aggregate_groups,
    compute_breadth,
    mad_winsorize,
    rank_group_metrics,
)
from src.group_analytics.confidence import compute_snapshot_quality, evaluate_ranking
from src.group_analytics.settings import DailyReturnSettings, RankingSettings


def _members(returns, *, group_id="g", group_name="Group"):
    return pd.DataFrame({
        "group_id": group_id,
        "group_name": group_name,
        "level": "sector",
        "security_id": [f"fmp:ticker:T{i}" for i in range(len(returns))],
        "counting_unit_id": [f"security:T{i}" for i in range(len(returns))],
        "ticker": [f"T{i}" for i in range(len(returns))],
        "name": [f"Test {i}" for i in range(len(returns))],
        "raw_return_1d": returns,
        "reason_codes": [[] for _ in returns],
    })


class RobustAggregationTests(unittest.TestCase):
    def test_equal_weight_median_and_mad_winsorized_return(self):
        values = [-0.02, -0.01, 0.0, 0.01, 1.0]

        result = aggregate_group_members(_members(values))

        self.assertAlmostEqual(result.metric["raw_ew_return_1d"], 0.196)
        self.assertAlmostEqual(result.metric["median_return_1d"], 0.0)
        self.assertAlmostEqual(result.metric["dispersion_mad"], 0.01)
        self.assertAlmostEqual(result.metric["robust_ew_return_1d"], 0.0048956)
        outlier = result.members.loc[result.members["ticker"] == "T4"].iloc[0]
        self.assertTrue(outlier["was_winsorized"])
        self.assertAlmostEqual(outlier["winsorized_return_1d"], 0.044478)

    def test_n_below_minimum_and_zero_mad_are_not_winsorized(self):
        small = mad_winsorize(pd.Series([-0.02, -0.01, 0.01, 1.0]))
        zero_mad = mad_winsorize(pd.Series([0.01, 0.01, 0.01, 0.01, 1.0]))

        self.assertFalse(small.applied)
        self.assertAlmostEqual(float(small.values.mean()), 0.245)
        self.assertFalse(zero_mad.applied)
        self.assertFalse(zero_mad.was_winsorized.any())

    def test_missing_return_is_not_zero_and_stays_in_expected_count(self):
        result = aggregate_group_members(
            _members([0.02, None, np.nan, np.inf, -np.inf, -0.01])
        )

        self.assertEqual(result.metric["n_expected"], 6)
        self.assertEqual(result.metric["n_valid"], 2)
        self.assertAlmostEqual(result.metric["count_coverage"], 1 / 3)
        self.assertAlmostEqual(result.metric["raw_ew_return_1d"], 0.005)
        self.assertEqual(len(result.members), 6)
        self.assertEqual(result.members["is_valid_for_headline"].sum(), 2)

    def test_contributions_reconcile_and_driver_uses_robust_value(self):
        result = aggregate_group_members(_members([-0.02, -0.01, 0.0, 0.01, 1.0]))

        self.assertAlmostEqual(
            result.contributions["contribution"].sum(),
            result.metric["robust_ew_return_1d"],
            places=12,
        )
        self.assertEqual(result.metric["driver_method"], "ROBUST_EW")
        top = result.contributions.sort_values(
            ["contribution", "ticker"], ascending=[False, True]
        ).iloc[0]
        self.assertEqual(result.metric["top_driver_ticker"], top["ticker"])

    def test_stage_one_cap_schema_never_falls_back_to_equal_weight(self):
        result = aggregate_group_members(_members([0.01] * 5))

        self.assertIsNone(result.metric["cap_return_1d"])
        self.assertEqual(result.metric["cap_type"], "UNAVAILABLE")
        self.assertEqual(result.metric["cap_status"], "UNAVAILABLE")

    def test_benchmark_unavailable_is_informational_not_ranking_blocker(self):
        result = aggregate_group_members(_members([0.01] * 5), benchmark_return_1d=None)

        self.assertTrue(result.metric["eligible_for_ranking"])
        self.assertIn("BENCHMARK_UNAVAILABLE", result.metric["reason_codes"])


class BreadthAndQualityTests(unittest.TestCase):
    def test_one_basis_point_boundaries_are_strict(self):
        result = compute_breadth(
            pd.Series([0.000101, 0.000100, 0.0, -0.000100, -0.000101, np.nan])
        )

        self.assertEqual(result["advance_count"], 1)
        self.assertEqual(result["decline_count"], 1)
        self.assertEqual(result["unchanged_count"], 3)
        self.assertAlmostEqual(result["up_pct"], 0.2)
        self.assertAlmostEqual(result["down_pct"], 0.2)
        self.assertEqual(result["breadth_net"], 0.0)
        self.assertEqual(result["ad_ratio"], 1.0)

    def test_quality_and_gate_boundaries(self):
        cases = [
            (10, 10, 100.0, "A", True),
            (10, 8, 84.0, "B", True),
            (10, 7, 76.0, "B", False),
            (5, 5, 90.0, "B", True),
            (4, 4, 88.0, "C", False),
        ]
        for expected, valid, score, grade, eligible in cases:
            with self.subTest(expected=expected, valid=valid):
                quality = compute_snapshot_quality(expected, valid)
                ranking = evaluate_ranking(quality)
                self.assertAlmostEqual(quality.snapshot_quality_score, score)
                self.assertEqual(quality.snapshot_quality_grade, grade)
                self.assertEqual(ranking.eligible_for_ranking, eligible)


class RankingTests(unittest.TestCase):
    def test_stable_tie_break_and_dynamic_non_overlapping_views(self):
        rows = [
            {"group_id": "b", "robust_ew_return_1d": .01, "up_pct": .70, "n_valid": 5, "eligible_for_ranking": True},
            {"group_id": "a", "robust_ew_return_1d": .01, "up_pct": .60, "n_valid": 20, "eligible_for_ranking": True},
            {"group_id": "c", "robust_ew_return_1d": .01, "up_pct": .60, "n_valid": 20, "eligible_for_ranking": True},
            {"group_id": "z", "robust_ew_return_1d": .01, "up_pct": .60, "n_valid": 10, "eligible_for_ranking": True},
            {"group_id": "weak", "robust_ew_return_1d": -.02, "up_pct": .20, "n_valid": 12, "eligible_for_ranking": True},
        ]
        random.Random(7).shuffle(rows)

        ranked, top, bottom = rank_group_metrics(pd.DataFrame(rows), top_n=5, bottom_n=5)

        eligible = ranked.loc[ranked["eligible_for_ranking"], "group_id"].tolist()
        self.assertEqual(eligible, ["b", "a", "c", "z", "weak"])
        self.assertEqual(top["group_id"].tolist(), ["b", "a"])
        self.assertEqual(bottom["group_id"].tolist(), ["weak", "z"])
        self.assertTrue(set(top["group_id"]).isdisjoint(bottom["group_id"]))

    def test_aggregate_groups_preserves_all_expected_members(self):
        frame = pd.concat([
            _members([0.01] * 5, group_id="g1", group_name="One"),
            _members([-0.01] * 5, group_id="g2", group_name="Two").assign(
                security_id=lambda x: "g2:" + x["security_id"],
                ticker=lambda x: "X" + x["ticker"],
            ),
        ], ignore_index=True)

        result = aggregate_groups(frame, benchmark_return_1d=0.0)

        self.assertEqual(len(result.metrics), 2)
        self.assertEqual(len(result.members), 10)
        self.assertEqual(len(result.contributions), 10)


if __name__ == "__main__":
    unittest.main()
