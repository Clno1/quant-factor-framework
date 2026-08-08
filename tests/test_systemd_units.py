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


if __name__ == "__main__":
    unittest.main()
