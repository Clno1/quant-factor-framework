from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from scripts.verify_momentum_discord_routing import (
    DiscordRoutingError,
    verify_routing,
)


MOMENTUM = "https://discord.com/api/webhooks/100/momentum-secret"
SECTOR = "https://discord.com/api/webhooks/200/sector-secret"


def _write(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )


class MomentumDiscordRoutingTests(unittest.TestCase):
    def _paths(self, root: Path) -> tuple[Path, Path, Path]:
        hourly = root / "hourly.env"
        premarket = root / "premarket.env"
        intraday = root / "intraday.env"
        _write(hourly, {"DISCORD_WEBHOOK_URL": MOMENTUM})
        _write(premarket, {
            "PREMARKET_SECTOR_ROTATION_ENABLED": "true",
            "DISCORD_MOMENTUM_WEBHOOK_URL": MOMENTUM,
            "DISCORD_SECTOR_ROTATION_WEBHOOK_URL": SECTOR,
        })
        _write(intraday, {
            "INTRADAY_MOMENTUM_DISCORD_ENABLED": "true",
            "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL": MOMENTUM,
        })
        return hourly, premarket, intraday

    def test_all_momentum_workers_share_the_canonical_momentum_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._paths(Path(temporary))
            verify_routing(
                "all",
                hourly_env=paths[0],
                premarket_env=paths[1],
                intraday_env=paths[2],
            )

    def test_hourly_sector_route_fails_without_exposing_the_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            hourly, premarket, intraday = self._paths(Path(temporary))
            _write(hourly, {"DISCORD_WEBHOOK_URL": SECTOR})

            with self.assertRaises(DiscordRoutingError) as raised:
                verify_routing(
                    "hourly",
                    hourly_env=hourly,
                    premarket_env=premarket,
                    intraday_env=intraday,
                )

            self.assertNotIn("sector-secret", str(raised.exception))

    def test_intraday_sector_route_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            hourly, premarket, intraday = self._paths(Path(temporary))
            _write(intraday, {
                "INTRADAY_MOMENTUM_DISCORD_ENABLED": "true",
                "INTRADAY_MOMENTUM_DISCORD_WEBHOOK_URL": SECTOR,
            })

            with self.assertRaisesRegex(
                DiscordRoutingError,
                "intraday momentum webhook does not match",
            ):
                verify_routing(
                    "intraday",
                    hourly_env=hourly,
                    premarket_env=premarket,
                    intraday_env=intraday,
                )

    def test_premarket_sector_must_use_an_independent_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            hourly, premarket, intraday = self._paths(Path(temporary))
            _write(premarket, {
                "PREMARKET_SECTOR_ROTATION_ENABLED": "true",
                "DISCORD_MOMENTUM_WEBHOOK_URL": MOMENTUM,
                "DISCORD_SECTOR_ROTATION_WEBHOOK_URL": MOMENTUM,
            })

            with self.assertRaisesRegex(DiscordRoutingError, "must be independent"):
                verify_routing(
                    "premarket",
                    hourly_env=hourly,
                    premarket_env=premarket,
                    intraday_env=intraday,
                )

    def test_enabled_sector_requires_a_webhook(self):
        with tempfile.TemporaryDirectory() as temporary:
            hourly, premarket, intraday = self._paths(Path(temporary))
            _write(premarket, {
                "PREMARKET_SECTOR_ROTATION_ENABLED": "true",
                "DISCORD_MOMENTUM_WEBHOOK_URL": MOMENTUM,
                "DISCORD_SECTOR_ROTATION_WEBHOOK_URL": "",
            })

            with self.assertRaisesRegex(DiscordRoutingError, "is required"):
                verify_routing(
                    "premarket",
                    hourly_env=hourly,
                    premarket_env=premarket,
                    intraday_env=intraday,
                )


if __name__ == "__main__":
    unittest.main()
