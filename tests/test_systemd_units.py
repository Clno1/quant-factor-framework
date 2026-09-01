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

    def test_removed_runlog_directory_is_optional_in_root_services(self):
        root_services = list(SYSTEMD_DIR.glob("quant-*-root.service"))
        for service in root_services:
            runlog_lines = [
                line
                for line in service.read_text(encoding="utf-8").splitlines()
                if "/home/projects/quant/runlog" in line
            ]
            self.assertEqual(
                runlog_lines,
                ["ReadWritePaths=-/home/projects/quant/runlog"],
                service.name,
            )

    def test_momentum_root_units_match_current_server_layout(self):
        hourly = (
            SYSTEMD_DIR / "quant-momentum-alerts-root.service"
        ).read_text(encoding="utf-8")
        intraday = (
            SYSTEMD_DIR / "quant-intraday-momentum-monitor-root.service"
        ).read_text(encoding="utf-8")
        prepare = (
            SYSTEMD_DIR / "quant-intraday-candidate-prepare-root.service"
        ).read_text(encoding="utf-8")
        refresh = (
            SYSTEMD_DIR / "quant-us-daily-refresh-root.service"
        ).read_text(encoding="utf-8")
        premarket = (
            SYSTEMD_DIR / "quant-premarket-digest-root.service"
        ).read_text(encoding="utf-8")
        premarket_prepare = (
            SYSTEMD_DIR / "quant-premarket-prepare-root.service"
        ).read_text(encoding="utf-8")

        self.assertIn("--scheduled-hourly", hourly)
        self.assertIn("--component hourly", hourly)
        self.assertIn("--auto", intraday)
        self.assertIn("--component intraday", intraday)
        self.assertIn("--prepare-candidates", prepare)
        self.assertIn("MemoryHigh=500M", prepare)
        self.assertIn("MemoryMax=700M", prepare)
        self.assertIn("MemorySwapMax=0", prepare)
        self.assertNotIn("--skip-precompute", refresh)
        self.assertIn("--channel all", premarket)
        self.assertIn("--prepare --channel all", premarket_prepare)
        self.assertNotIn("--send", premarket_prepare)
        self.assertIn("MemoryHigh=500M", premarket_prepare)
        self.assertIn("MemoryMax=700M", premarket_prepare)
        self.assertIn("MemorySwapMax=0", premarket_prepare)
        self.assertIn("MemoryHigh=300M", premarket)
        self.assertIn("MemoryMax=450M", premarket)
        self.assertIn("--component premarket", premarket)
        self.assertIn("PREMARKET_SECTOR_ROTATION_ENABLED=true", premarket)
        self.assertNotIn("quant-group-analytics-eod.service", premarket)
        for content in (hourly, intraday, premarket):
            self.assertIn("quant-us-equity-coverage.service", content)
            self.assertNotIn("quant-us-daily-refresh.service", content)

        group_timer = (
            SYSTEMD_DIR / "quant-group-analytics-eod.timer"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "OnCalendar=Tue..Sat *-*-* 13:15:00 Asia/Singapore",
            group_timer,
        )
        candidate_timer = (
            SYSTEMD_DIR / "quant-intraday-candidate-prepare.timer"
        ).read_text(encoding="utf-8")
        self.assertIn("06:30:00 America/New_York", candidate_timer)
        premarket_prepare_timer = (
            SYSTEMD_DIR / "quant-premarket-prepare.timer"
        ).read_text(encoding="utf-8")
        self.assertIn("07:00:00 America/New_York", premarket_prepare_timer)

    def test_broad_units_form_a_resource_bounded_success_chain(self):
        coverage = (
            SYSTEMD_DIR / "quant-us-equity-coverage-root.service"
        ).read_text(encoding="utf-8")
        factor = (
            SYSTEMD_DIR / "quant-broad-factor-data-root.service"
        ).read_text(encoding="utf-8")
        readiness = (
            SYSTEMD_DIR / "quant-broad-research-readiness-root.service"
        ).read_text(encoding="utf-8")
        shadow = (
            SYSTEMD_DIR / "quant-broad-shadow-observation-root.service"
        ).read_text(encoding="utf-8")
        timer = (
            SYSTEMD_DIR / "quant-us-equity-coverage.timer"
        ).read_text(encoding="utf-8")
        initial = (
            SYSTEMD_DIR / "quant-broad-initial-rollout-root.service"
        ).read_text(encoding="utf-8")
        initial_timer = (
            SYSTEMD_DIR / "quant-broad-initial-rollout-scheduled.timer"
        ).read_text(encoding="utf-8")

        self.assertIn("OnSuccess=quant-broad-factor-data.service", coverage)
        self.assertIn("quant-broad-research-readiness.service", factor)
        self.assertIn("--auto-resume", factor)
        self.assertIn("--restart-after-partitions 1", factor)
        self.assertNotIn("quant-broad-shadow-observation.service", factor)
        self.assertIn(
            "OnSuccess=quant-broad-shadow-observation.service",
            readiness,
        )
        self.assertNotIn("Requires=quant-us-equity-coverage.service", factor)
        self.assertNotIn("Requires=quant-broad-factor-data.service", readiness)
        for content in (coverage, factor):
            self.assertIn("check_broad_resources.py", content)
            self.assertIn("OMP_NUM_THREADS=1", content)
            self.assertIn("MemoryHigh=700M", content)
            self.assertIn("MemoryMax=900M", content)
            self.assertIn(
                "/home/projects/quant/data/lake/.broad-production.lock",
                content,
            )
        self.assertIn("SuccessExitStatus=2", readiness)
        self.assertNotIn("SuccessExitStatus=2", shadow)
        self.assertIn("check_broad_shadow_observation.py", shadow)
        self.assertIn(
            "OnCalendar=Tue..Sat *-*-* 11:30:00 Asia/Singapore",
            timer,
        )
        self.assertIn("run_broad_initial_rollout.py", initial)
        self.assertIn("MemoryHigh=700M", initial)
        self.assertIn("MemoryMax=900M", initial)
        self.assertIn("TimeoutStartSec=36h", initial)
        self.assertIn("Restart=on-failure", initial)
        self.assertNotIn("--enable-daily-timer-on-success", initial)
        self.assertIn(
            "OnCalendar=2026-08-15 11:35:00 Asia/Singapore",
            initial_timer,
        )
        self.assertIn("Persistent=true", initial_timer)
        self.assertIn("Unit=quant-broad-initial-rollout.service", initial_timer)

    def test_core_market_recovery_is_narrow_and_resource_bounded(self):
        service = (
            SYSTEMD_DIR / "quant-market-data-root.service"
        ).read_text(encoding="utf-8")
        self.assertIn("scripts/run_core_market_data.py", service)
        self.assertIn("--workers 4", service)
        self.assertIn("OMP_NUM_THREADS=1", service)
        self.assertIn("MemoryHigh=700M", service)
        self.assertIn("MemoryMax=900M", service)
        self.assertIn("MemorySwapMax=0", service)
        self.assertIn(".broad-production.lock", service)

    def test_research_jobs_are_resource_bounded(self):
        factor = (
            SYSTEMD_DIR / "quant-factor-research-root.service"
        ).read_text(encoding="utf-8")
        group = (
            SYSTEMD_DIR / "quant-group-analytics-eod-root.service"
        ).read_text(encoding="utf-8")
        broad_factor = (
            SYSTEMD_DIR / "quant-broad-factor-data-root.service"
        ).read_text(encoding="utf-8")

        for value in (factor, group):
            self.assertIn("MemorySwapMax=0", value)
            self.assertIn("CPUQuota=100%", value)
            self.assertIn("TasksMax=64", value)
            self.assertIn("OOMPolicy=stop", value)
        self.assertIn("MemoryHigh=700M", factor)
        self.assertIn("MemoryMax=900M", factor)
        self.assertIn("MemoryHigh=400M", group)
        self.assertIn("MemoryMax=550M", group)
        self.assertIn("quant-broad-factor-data.service", group)
        self.assertIn(".broad-production.lock", group)
        self.assertIn(
            "Before=quant-group-analytics-eod.service",
            broad_factor,
        )

    def test_operations_site_is_independent_and_read_only(self):
        web = (
            SYSTEMD_DIR / "quant-operations-web-root.service"
        ).read_text(encoding="utf-8")
        watchdog = (
            SYSTEMD_DIR / "quant-operations-watchdog-root.service"
        ).read_text(encoding="utf-8")
        timer = (
            SYSTEMD_DIR / "quant-operations-watchdog.timer"
        ).read_text(encoding="utf-8")

        self.assertIn("--host 0.0.0.0 --port 18825", web)
        self.assertIn("EnvironmentFile=/etc/quant/operations-web.env", web)
        self.assertNotIn("EnvironmentFile=-/etc/quant/operations-web.env", web)
        self.assertNotIn("/etc/quant/web.env", web)
        self.assertIn("ReadOnlyPaths=/home/projects/quant", web)
        self.assertIn("MemoryMax=180M", web)
        self.assertNotIn("Discord", watchdog)
        self.assertIn("run_operations_watchdog.py", watchdog)
        self.assertIn(
            "ReadWritePaths=/home/projects/quant/outputs/operations",
            watchdog,
        )
        self.assertIn("MemoryMax=260M", watchdog)
        self.assertEqual(_setting(SYSTEMD_DIR / "quant-operations-watchdog.timer", "OnUnitInactiveSec"), "1min")

    def test_main_web_has_a_host_protecting_memory_boundary(self):
        web = (SYSTEMD_DIR / "quant-web-root.service").read_text(encoding="utf-8")

        self.assertIn("MemoryHigh=420M", web)
        self.assertIn("MemoryMax=600M", web)
        self.assertIn("MemorySwapMax=0", web)
        self.assertIn("OOMPolicy=stop", web)

    def test_sg_release_excludes_only_root_state_directories(self):
        guide = (PROJECT_ROOT / "docs" / "sg_manual_deploy_3a52611.md").read_text(
            encoding="utf-8"
        )

        for directory in ("data", "outputs", "logs", "runlog"):
            self.assertIn(f"--exclude='/{directory}/'", guide)
            self.assertNotIn(f"--exclude='{directory}/'", guide)

if __name__ == "__main__":
    unittest.main()
