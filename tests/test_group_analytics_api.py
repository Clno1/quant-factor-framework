from __future__ import annotations

import builtins
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pandas as pd
from fastapi import FastAPI
try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):  # optional requirements-dev.txt dependency
    TestClient = None  # type: ignore[assignment,misc]

from src.group_analytics.aggregation import aggregate_group_members
from src.group_analytics.artifacts import ArtifactReader, FileGroupArtifactStore
from src.group_analytics.models import ArtifactCombination, GroupAnalyticsBundle
from src.group_analytics.settings import GroupAnalyticsSettings, RankingSettings
from src.webapp import group_analytics_routes


ASOF = "2026-07-15"


def _bundle(
    *,
    level: str,
    groups: list[tuple[str, str, float]],
) -> GroupAnalyticsBundle:
    metrics: list[pd.DataFrame] = []
    members: list[pd.DataFrame] = []
    contributions: list[pd.DataFrame] = []

    for group_index, (group_id, group_name, center_return) in enumerate(groups):
        tickers = [f"T{group_index}{member_index}" for member_index in range(5)]
        source = pd.DataFrame(
            {
                "group_id": [group_id] * 5,
                "group_name": [group_name] * 5,
                "level": [level] * 5,
                "security_id": [f"security:{ticker}" for ticker in tickers],
                "counting_unit_id": [f"security:{ticker}" for ticker in tickers],
                "ticker": tickers,
                "name": [f"Test security {ticker}" for ticker in tickers],
                "raw_return_1d": [
                    center_return - 0.002,
                    center_return - 0.001,
                    center_return,
                    center_return + 0.001,
                    center_return + 0.002,
                ],
                "reason_codes": [[] for _ in range(5)],
            }
        )
        aggregated = aggregate_group_members(
            source,
            benchmark_return_1d=0.001,
        )
        metrics.append(pd.DataFrame([aggregated.metric]))
        members.append(aggregated.members)
        contributions.append(aggregated.contributions)

    return GroupAnalyticsBundle(
        metrics=pd.concat(metrics, ignore_index=True),
        members=pd.concat(members, ignore_index=True),
        contributions=pd.concat(contributions, ignore_index=True),
        diagnostics={
            "missing_members": [
                {"ticker": "MISS1", "reason_code": "MISSING_RETURN"},
                {"ticker": "MISS2", "reason_code": "MISSING_CLASSIFICATION"},
            ],
            "low_confidence_groups": [
                {"group_id": groups[-1][0], "reason_codes": ["SMALL_GROUP"]}
            ],
            "classification_diagnostics": [
                {
                    "ticker": "GOOG",
                    "reason_code": "SHARE_CLASS_DEDUPED",
                    "provenance_path": "/Users/private/cache.json",
                    "api_key": "must-not-leak",
                }
            ],
        },
        manifest={
            "asof": ASOF,
            "source_max_date": ASOF,
            "snapshot_time": f"{ASOF}T20:00:00Z",
            "input_fingerprint": f"sha256:test-{level}",
            "universe_version": "test-sp500-v1",
            "taxonomy_version": "test-fmp-v1",
            "classification_asof": ASOF,
            "classification_hash": "sha256:test-classification",
            "classification_provider": "FMP",
            "group_id_mapping_version": "test-group-id-map-v1",
            "fallback": False,
            "fetched_at": f"{ASOF}T19:30:00Z",
            "benchmark": "SPY",
            "counting_unit": "security_with_overrides",
            "issuer_dedupe_status": "PARTIAL_OVERRIDES",
            "issuer_overrides_applied": True,
            "issuer_override_count": 1,
            "issuer_override_version": "test-overrides-v1",
            "pit_universe_applied": False,
            "pit_classification_applied": False,
            "freshness_status": "FRESH",
            "quality_status": "OK",
        },
        run={
            "asof": ASOF,
            "freshness_status": "FRESH",
            "quality_status": "OK",
            "reason_codes": [],
        },
    )


@unittest.skipIf(TestClient is None, "httpx is installed from requirements-dev.txt")
class GroupAnalyticsAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.settings = GroupAnalyticsSettings(
            enabled=True,
            output_root=Path(self.temporary_directory.name),
            ranking=RankingSettings(top_n=5, bottom_n=5),
        )
        self.store = FileGroupArtifactStore(self.settings)
        self.reader = ArtifactReader(self.settings)

        self.original_reader = group_analytics_routes._READER
        self.original_settings = group_analytics_routes.settings
        group_analytics_routes._READER = self.reader
        group_analytics_routes.settings = self.settings
        self.addCleanup(self._restore_route_dependencies)

        app = FastAPI()
        app.include_router(group_analytics_routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def _restore_route_dependencies(self) -> None:
        group_analytics_routes._READER = self.original_reader
        group_analytics_routes.settings = self.original_settings

    def _publish(
        self,
        *,
        run_id: str,
        level: str,
        groups: list[tuple[str, str, float]],
    ) -> ArtifactCombination:
        combination = ArtifactCombination("SP500", "FMP", level, "eod")
        outcome = self.store.publish(
            run_id=run_id,
            combination=combination,
            bundle=_bundle(level=level, groups=groups),
        )
        self.assertTrue(outcome.published)
        return combination

    def test_metadata_lists_only_actually_published_combinations(self) -> None:
        self._publish(
            run_id="metadata-subindustry",
            level="sub_industry",
            groups=[("fmp:sub_industry:software", "Software", 0.02)],
        )

        response = self.client.get("/api/group-analytics/metadata")

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        combinations = payload["available_combinations"]
        self.assertEqual(len(combinations), 1)
        self.assertEqual(
            {
                key: combinations[0][key]
                for key in ("universe", "taxonomy", "level", "mode")
            },
            {
                "universe": "SP500",
                "taxonomy": "FMP",
                "level": "sub_industry",
                "mode": "eod",
            },
        )
        self.assertEqual(combinations[0]["latest_run_id"], "metadata-subindustry")
        self.assertEqual(combinations[0]["benchmarks"], ["SPY"])
        self.assertEqual(combinations[0]["return_methods"], ["ROBUST_EW"])

    def test_heat_envelope_and_dynamic_top_bottom_are_disjoint(self) -> None:
        groups = [
            ("fmp:sector:technology", "Technology", 0.04),
            ("fmp:sector:financials", "Financials", 0.02),
            ("fmp:sector:utilities", "Utilities", -0.01),
            ("fmp:sector:energy", "Energy", -0.03),
        ]
        self._publish(run_id="heat-sector", level="sector", groups=groups)

        top_response = self.client.get(
            "/api/group-analytics/heat",
            params={"level": "sector", "view": "top"},
        )
        bottom_response = self.client.get(
            "/api/group-analytics/heat",
            params={"level": "sector", "view": "bottom"},
        )

        self.assertEqual(top_response.status_code, 200, top_response.text)
        self.assertEqual(bottom_response.status_code, 200, bottom_response.text)
        top = top_response.json()
        bottom = bottom_response.json()
        for payload in (top, bottom):
            self.assertEqual(payload["data_run_id"], "heat-sector")
            self.assertEqual(payload["universe"], "SP500")
            self.assertEqual(payload["taxonomy"], "FMP")
            self.assertEqual(payload["taxonomy_level"], "sector")
            self.assertEqual(payload["mode"], "eod")
            self.assertEqual(payload["session_status"], "FINAL")
            self.assertEqual(payload["methodology"]["headline_method"], "ROBUST_EW")
            self.assertEqual(payload["last_attempt_status"], "SUCCESS")
            self.assertEqual(payload["benchmark_return_1d"], 0.001)
            self.assertEqual(payload["group_id_mapping_version"], "test-group-id-map-v1")
            self.assertEqual(len(payload["rows"]), 2)
            for row in payload["rows"]:
                self.assertIn("view_rank", row)
                self.assertIn("headline_rank", row)
                self.assertIsNone(row["cap_return_1d"])
                self.assertEqual(row["cap_type"], "UNAVAILABLE")

        top_ids = {row["group_id"] for row in top["rows"]}
        bottom_ids = {row["group_id"] for row in bottom["rows"]}
        self.assertEqual(
            top_ids,
            {"fmp:sector:technology", "fmp:sector:financials"},
        )
        self.assertEqual(
            bottom_ids,
            {"fmp:sector:utilities", "fmp:sector:energy"},
        )
        self.assertEqual(
            [row["group_id"] for row in top["rows"]],
            ["fmp:sector:technology", "fmp:sector:financials"],
        )
        self.assertEqual(
            [row["group_id"] for row in bottom["rows"]],
            ["fmp:sector:energy", "fmp:sector:utilities"],
        )
        self.assertTrue(top_ids.isdisjoint(bottom_ids))

    def test_detail_pins_requested_subindustry_run_and_paginates_members(self) -> None:
        old_group_id = "fmp:sub_industry:application-software"
        self._publish(
            run_id="subindustry-old",
            level="sub_industry",
            groups=[(old_group_id, "Application Software", 0.03)],
        )
        self._publish(
            run_id="subindustry-new",
            level="sub_industry",
            groups=[("fmp:sub_industry:semiconductors", "Semiconductors", 0.01)],
        )

        response = self.client.get(
            f"/api/group-analytics/groups/{old_group_id}",
            params={
                "level": "sub_industry",
                "data_run_id": "subindustry-old",
                "page": 2,
                "page_size": 2,
                "member_sort_by": "ticker",
                "member_sort_order": "asc",
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data_run_id"], "subindustry-old")
        self.assertEqual(payload["summary"]["group_id"], old_group_id)
        self.assertEqual(payload["summary"]["level"], "sub_industry")
        self.assertEqual(payload["asof"], ASOF)
        self.assertEqual(payload["provenance"]["taxonomy"], "FMP")
        self.assertEqual(payload["provenance"]["taxonomy_level"], "sub_industry")
        self.assertEqual(payload["provenance"]["classification_asof"], ASOF)
        self.assertEqual(
            payload["provenance"]["classification_hash"],
            "sha256:test-classification",
        )
        self.assertEqual(payload["provenance"]["classification_provider"], "FMP")
        self.assertFalse(payload["provenance"]["fallback"])
        self.assertEqual(payload["provenance"]["fetched_at"], f"{ASOF}T19:30:00Z")
        self.assertEqual(
            payload["provenance"]["group_id_mapping_version"],
            "test-group-id-map-v1",
        )
        page = payload["members"]
        self.assertEqual(page["page"], 2)
        self.assertEqual(page["page_size"], 2)
        self.assertEqual(page["total"], 5)
        self.assertTrue(page["has_next"])
        self.assertEqual(len(page["rows"]), 2)
        self.assertTrue(all(row["level"] == "sub_industry" for row in page["rows"]))
        self.assertTrue(all(row["security_id"] for row in page["rows"]))
        self.assertTrue(all("contribution_bps" in row for row in page["rows"]))
        self.assertEqual([row["ticker"] for row in page["rows"]], ["T02", "T03"])
        self.assertEqual(len(payload["contribution_drivers"]["top_positive"]), 5)
        self.assertEqual(payload["contribution_drivers"]["top_negative"], [])
        self.assertEqual(payload["distribution"]["n_winsorized"], 0)
        self.assertIsNotNone(payload["distribution"]["winsor_lower"])
        self.assertIsNotNone(payload["distribution"]["winsor_upper"])

    def test_pages_include_accessible_heat_and_detail_controls(self) -> None:
        heat_page = self.client.get("/group-analytics")
        detail_page = self.client.get(
            "/group-analytics/groups/fmp:sector:technology",
            params={"level": "sector", "data_run_id": "safe-run-id"},
        )

        self.assertEqual(heat_page.status_code, 200, heat_page.text)
        self.assertIn('id="ga-heatmap"', heat_page.text)
        self.assertIn('role="list"', heat_page.text)
        self.assertIn('id="ga-auto-scale"', heat_page.text)
        self.assertIn('id="ga-status-details"', heat_page.text)
        self.assertIn("市值加权 1D", heat_page.text)
        self.assertIn("视图/总榜排名", heat_page.text)

        self.assertEqual(detail_page.status_code, 200, detail_page.text)
        self.assertIn('id="ga-detail-provenance"', detail_page.text)
        self.assertIn('id="ga-member-sort-by"', detail_page.text)
        self.assertIn('id="ga-member-search"', detail_page.text)
        self.assertIn("Security ID", detail_page.text)
        self.assertIn("贡献 (bp)", detail_page.text)

    def test_runs_endpoint_returns_paginated_diagnostics(self) -> None:
        self._publish(
            run_id="diagnostics-run",
            level="sector",
            groups=[("fmp:sector:technology", "Technology", 0.02)],
        )

        response = self.client.get(
            "/api/group-analytics/runs/diagnostics-run",
            params={
                "diagnostic_type": "missing_members",
                "page": 1,
                "page_size": 1,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["run_id"], "diagnostics-run")
        self.assertEqual(payload["last_attempt_status"], "SUCCESS")
        self.assertEqual(payload["diagnostic_counts"]["missing_members"], 2)
        diagnostics = payload["diagnostics"]
        self.assertEqual(diagnostics["diagnostic_type"], "missing_members")
        self.assertEqual(diagnostics["total"], 2)
        self.assertTrue(diagnostics["has_next"])
        self.assertEqual(len(diagnostics["rows"]), 1)
        self.assertEqual(diagnostics["rows"][0]["ticker"], "MISS1")

        redacted = self.client.get(
            "/api/group-analytics/runs/diagnostics-run",
            params={"diagnostic_type": "classification_diagnostics"},
        )
        self.assertEqual(redacted.status_code, 200, redacted.text)
        serialized = redacted.text
        self.assertNotIn("/Users/private", serialized)
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn("provenance_path", serialized)
        self.assertNotIn("api_key", serialized)

    def test_invalid_enum_and_unknown_query_are_contract_422(self) -> None:
        invalid_enum = self.client.get(
            "/api/group-analytics/heat",
            params={"level": "industry"},
        )
        unknown_query = self.client.get(
            "/api/group-analytics/metadata",
            params={"unexpected": "value"},
        )

        for response in (invalid_enum, unknown_query):
            self.assertEqual(response.status_code, 422, response.text)
            payload = response.json()
            self.assertEqual(payload["error"]["code"], "INVALID_REQUEST")
            self.assertTrue(payload["error"]["request_id"].startswith("req_"))
        self.assertEqual(
            invalid_enum.json()["error"]["details"]["allowed_values"],
            ["sector", "sub_industry"],
        )
        self.assertEqual(
            unknown_query.json()["error"]["details"]["unknown_parameters"],
            ["unexpected"],
        )

    def test_unsafe_path_payloads_are_rejected_before_artifact_access(self) -> None:
        unsafe_group = self.client.get(
            "/api/group-analytics/groups/bad%3Cscript%3E"
        )
        unsafe_run = self.client.get(
            "/api/group-analytics/runs/bad%5C..%5Csecret"
        )

        for response in (unsafe_group, unsafe_run):
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_eod_freshness_uses_exchange_session_and_publish_sla(self) -> None:
        class FakeCalendar:
            def sessions_in_range(self, _start, _end):
                return pd.DatetimeIndex(["2026-07-14", "2026-07-15"])

            def session_close(self, session):
                label = pd.Timestamp(session).tz_localize(None).normalize()
                return label.tz_localize("UTC") + pd.Timedelta(hours=20)

        manifest = {"asof": "2026-07-14"}
        delayed = group_analytics_routes._derive_eod_freshness(
            manifest,
            "FRESH",
            mode="eod",
            now=pd.Timestamp("2026-07-15T21:00:00Z"),
            calendar=FakeCalendar(),
        )
        stale = group_analytics_routes._derive_eod_freshness(
            manifest,
            "FRESH",
            mode="eod",
            now=pd.Timestamp("2026-07-15T23:01:00Z"),
            calendar=FakeCalendar(),
        )
        current = group_analytics_routes._derive_eod_freshness(
            {"asof": "2026-07-15"},
            "FRESH",
            mode="eod",
            now=pd.Timestamp("2026-07-18T12:00:00Z"),
            calendar=FakeCalendar(),
        )

        self.assertEqual(delayed, "DELAYED")
        self.assertEqual(stale, "STALE")
        self.assertEqual(current, "FRESH")

    def test_failed_only_combination_returns_503(self) -> None:
        combination = ArtifactCombination("SP500", "FMP", "sector", "eod")
        self.store.record_failure(
            "failed-only",
            combination,
            {"code": "INPUT_COVERAGE_FAILED", "stage": "load_inputs", "summary": "missing data"},
        )

        response = self.client.get("/api/group-analytics/heat")

        self.assertEqual(response.status_code, 503, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "NO_SUCCESSFUL_RUN")
        self.assertEqual(error["details"]["last_attempt_run_id"], "failed-only")
        self.assertEqual(error["details"]["last_attempt_status"], "FAILED")

    def test_failed_attempt_after_success_keeps_old_data_and_warning(self) -> None:
        combination = self._publish(
            run_id="stable-success",
            level="sector",
            groups=[("fmp:sector:technology", "Technology", 0.02)],
        )
        self.store.record_failure(
            "newer-failure",
            combination,
            {"code": "UPSTREAM_UNAVAILABLE", "stage": "load_inputs", "summary": "supplier unavailable"},
        )

        response = self.client.get(
            "/api/group-analytics/heat",
            params={"view_min_members": 0},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["data_run_id"], "stable-success")
        self.assertEqual(payload["last_attempt_run_id"], "newer-failure")
        self.assertEqual(payload["last_attempt_status"], "FAILED")
        self.assertEqual(payload["freshness_status"], "DELAYED")
        self.assertIn("FAILED_LAST_ATTEMPT", payload["reason_codes"])

    def test_api_requests_never_import_provider_or_computation_service(self) -> None:
        self._publish(
            run_id="reader-only",
            level="sector",
            groups=[("fmp:sector:technology", "Technology", 0.02)],
        )
        forbidden = (
            "src.group_analytics.service",
            "src.group_analytics.adapters",
            "src.group_analytics.classification",
        )
        original_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden):
                raise AssertionError(f"API imported forbidden runtime dependency: {name}")
            return original_import(name, globals, locals, fromlist, level)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            metadata_response = self.client.get("/api/group-analytics/metadata")
            heat_response = self.client.get(
                "/api/group-analytics/heat",
                params={"view_min_members": 0},
            )
            run_response = self.client.get("/api/group-analytics/runs/reader-only")

        self.assertEqual(metadata_response.status_code, 200, metadata_response.text)
        self.assertEqual(heat_response.status_code, 200, heat_response.text)
        self.assertEqual(run_response.status_code, 200, run_response.text)


if __name__ == "__main__":
    unittest.main()
