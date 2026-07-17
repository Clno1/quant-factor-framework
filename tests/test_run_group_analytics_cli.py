from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts import run_group_analytics as cli
from src.group_analytics.models import (
    ArtifactCombination,
    RunOutcome,
    RunStatus,
)


def _outcome(level: str, status: RunStatus = RunStatus.SUCCESS) -> RunOutcome:
    return RunOutcome(
        run_id=f"run-{level}",
        status=status,
        dry_run=False,
        published=status == RunStatus.SUCCESS,
        combination=ArtifactCombination("SP500", "FMP", level, "eod"),
        asof="2026-07-15",
        artifact_locator=(
            f"universes/SP500/FMP/{level}/eod/runs/run-{level}"
            if status == RunStatus.SUCCESS
            else None
        ),
        error=(
            None
            if status == RunStatus.SUCCESS
            else {"code": "LEVEL_FAILED", "message": "fixture failure"}
        ),
    )


class RunGroupAnalyticsCliTests(unittest.TestCase):
    def _run(self, service: Mock, argv: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with (
            patch.object(cli, "load_group_analytics_settings", return_value=object()),
            patch.object(cli, "GroupAnalyticsService", return_value=service),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            exit_code = cli.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_all_runs_both_levels_and_reports_nonzero_if_one_outcome_failed(self):
        service = Mock()
        service.run.side_effect = [
            _outcome("sector", RunStatus.FAILED),
            _outcome("sub_industry"),
        ]

        exit_code, stdout, stderr = self._run(service, ["--level", "all"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(
            [call.args[0].level for call in service.run.call_args_list],
            ["sector", "sub_industry"],
        )
        payload = json.loads(stdout)
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["summary"], {
            "failed": 1,
            "requested": 2,
            "succeeded_or_skipped": 1,
        })
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["FAILED", "SUCCESS"],
        )

    def test_all_continues_after_unexpected_error_and_redacts_exception_text(self):
        service = Mock()
        service.run.side_effect = [
            RuntimeError(
                "failed at /Users/operator/private/data with secret-key=abc123"
            ),
            _outcome("sub_industry"),
        ]

        exit_code, stdout, stderr = self._run(service, ["--level", "all"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        self.assertEqual(service.run.call_count, 2)
        self.assertNotIn("/Users/", stdout)
        self.assertNotIn("abc123", stdout)
        error = json.loads(stdout)["results"][0]["error"]
        self.assertEqual(error["code"], "INTERNAL_ERROR")
        self.assertEqual(error["details"], {"exception_type": "RuntimeError"})

    def test_single_level_unexpected_error_is_structured_on_stderr(self):
        service = Mock()
        service.run.side_effect = OSError("/opt/quant/private/input.parquet")

        exit_code, stdout, stderr = self._run(service, ["--level", "sector"])

        self.assertEqual(exit_code, 1)
        self.assertEqual(stdout, "")
        self.assertNotIn("/opt/quant", stderr)
        payload = json.loads(stderr)
        self.assertEqual(payload["error"]["code"], "INTERNAL_ERROR")
        self.assertEqual(payload["status"], "FAILED")

    def test_systemd_uses_one_all_level_writer_only_command(self):
        service_file = (
            Path(__file__).resolve().parents[1]
            / "deploy/systemd/quant-group-analytics-eod.service"
        )
        content = service_file.read_text(encoding="utf-8")

        self.assertEqual(content.count("\nExecStart="), 1)
        self.assertIn("--level all", content)
        self.assertIn("EnvironmentFile=-/etc/quant/momentum-alerts.env", content)
        self.assertIn("Environment=GROUP_ANALYTICS_ENABLED=true", content)
        self.assertIn("Environment=GROUP_ANALYTICS_WEB_ENABLED=false", content)
        self.assertIn("Restart=on-failure", content)
        self.assertIn("RestartSec=15min", content)

        refresh_service = service_file.with_name("quant-us-daily-refresh.service")
        refresh_content = refresh_service.read_text(encoding="utf-8")
        self.assertIn("--market-symbol SPY", refresh_content)


if __name__ == "__main__":
    unittest.main()
