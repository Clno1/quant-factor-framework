from __future__ import annotations

from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = PROJECT_ROOT / "deploy" / "systemd"


def _setting(path: Path, name: str) -> str:
    prefix = f"{name}="
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    raise AssertionError(f"{name} is missing from {path}")


class SystemdUnitTests(unittest.TestCase):
    def test_data_request_rate_limit_allows_every_timer_start(self):
        timer = SYSTEMD_DIR / "quant-data-requests.timer"
        self.assertEqual(_setting(timer, "OnUnitInactiveSec"), "5min")

        expected_hourly_starts = 12
        for name in (
            "quant-data-requests.service",
            "quant-data-requests-root.service",
        ):
            service = SYSTEMD_DIR / name
            self.assertEqual(_setting(service, "StartLimitIntervalSec"), "1h")
            self.assertGreaterEqual(
                int(_setting(service, "StartLimitBurst")),
                expected_hourly_starts * 2,
            )

    def test_root_services_use_the_single_production_venv(self):
        root_services = list(SYSTEMD_DIR.glob("quant-*-root.service"))
        self.assertGreaterEqual(len(root_services), 10)
        for service in root_services:
            content = service.read_text(encoding="utf-8")
            self.assertNotIn(".venv-worker", content, service.name)
            self.assertIn("/home/projects/quant/.venv/bin/python", content, service.name)

    def test_momentum_root_units_match_current_server_layout(self):
        hourly = (
            SYSTEMD_DIR / "quant-momentum-alerts-root.service"
        ).read_text(encoding="utf-8")
        intraday = (
            SYSTEMD_DIR / "quant-intraday-momentum-monitor-root.service"
        ).read_text(encoding="utf-8")
        refresh = (
            SYSTEMD_DIR / "quant-us-daily-refresh-root.service"
        ).read_text(encoding="utf-8")
        premarket = (
            SYSTEMD_DIR / "quant-premarket-digest-root.service"
        ).read_text(encoding="utf-8")

        self.assertIn("--scheduled-hourly", hourly)
        self.assertIn("--auto", intraday)
        self.assertNotIn("--skip-precompute", refresh)
        self.assertIn("--channel momentum", premarket)
        self.assertIn("PREMARKET_SECTOR_ROTATION_ENABLED=false", premarket)
        self.assertNotIn("quant-group-analytics-eod.service", premarket)


if __name__ == "__main__":
    unittest.main()
