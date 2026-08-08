from __future__ import annotations

import ast
from pathlib import Path
import unittest

from src.breakouts.application import (
    UnknownBreakoutUniverseError,
    normalize_breakout_universe,
    resolve_breakout_universe,
)
from src.config import PROJECT_ROOT


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


class BreakoutIsolationTests(unittest.TestCase):
    def test_breakout_domain_does_not_import_factor_or_web_domains(self):
        forbidden = (
            "src.alerts",
            "src.analysis",
            "src.backtest",
            "src.factors",
            "src.papertrading",
            "src.preprocessing",
            "src.strategies",
            "src.webapp",
        )
        violations: list[str] = []
        for path in (PROJECT_ROOT / "src" / "breakouts").rglob("*.py"):
            for imported in _imports(path):
                if any(
                    imported == prefix or imported.startswith(prefix + ".")
                    for prefix in forbidden
                ):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)} -> {imported}"
                    )
        self.assertEqual(violations, [])

    def test_factor_domain_does_not_import_breakouts_or_alerts(self):
        forbidden = ("src.breakouts", "src.alerts")
        violations: list[str] = []
        for path in (PROJECT_ROOT / "src" / "factors").rglob("*.py"):
            for imported in _imports(path):
                if any(
                    imported == prefix or imported.startswith(prefix + ".")
                    for prefix in forbidden
                ):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)} -> {imported}"
                    )
        self.assertEqual(violations, [])

    def test_intraday_monitor_does_not_import_web_or_factor_domains(self):
        forbidden = (
            "src.alerts",
            "src.analysis",
            "src.backtest",
            "src.factors",
            "src.papertrading",
            "src.preprocessing",
            "src.strategies",
            "src.webapp",
        )
        violations: list[str] = []
        live_root = PROJECT_ROOT / "src" / "breakouts" / "live"
        for path in live_root.rglob("*.py"):
            for imported in _imports(path):
                if any(
                    imported == prefix or imported.startswith(prefix + ".")
                    for prefix in forbidden
                ):
                    violations.append(
                        f"{path.relative_to(PROJECT_ROOT)} -> {imported}"
                    )
        self.assertEqual(violations, [])

    def test_refresh_worker_no_longer_imports_web_routes(self):
        imports = _imports(PROJECT_ROOT / "scripts" / "refresh_us_active.py")
        self.assertFalse(
            any(name == "src.webapp" or name.startswith("src.webapp.") for name in imports)
        )
        self.assertIn("src.breakouts.application", imports)

    def test_breakout_routes_have_single_owner(self):
        from src.webapp.breakout_routes import router as breakout_router
        from src.webapp.routes_v2 import router_v2

        expected = {
            "/breakouts",
            "/breakouts/{ticker}",
            "/api/breakouts/scan",
            "/api/breakouts/check/{ticker}",
            "/api/breakouts/{ticker}/intraday",
        }
        breakout_paths = {route.path for route in breakout_router.routes}
        legacy_paths = {route.path for route in router_v2.routes}
        self.assertEqual(breakout_paths, expected)
        self.assertFalse(any(path.startswith("/breakouts") for path in legacy_paths))
        self.assertFalse(any(path.startswith("/api/breakouts") for path in legacy_paths))

    def test_composition_root_registers_each_breakout_route_once(self):
        from src.webapp.app import create_app

        paths = [route.path for route in create_app().routes]
        expected = {
            "/breakouts",
            "/breakouts/{ticker}",
            "/api/breakouts/scan",
            "/api/breakouts/check/{ticker}",
            "/api/breakouts/{ticker}/intraday",
        }
        self.assertTrue(all(paths.count(path) == 1 for path in expected))

    def test_universe_normalization_preserves_existing_contract(self):
        self.assertEqual(normalize_breakout_universe(None), "US_ACTIVE")
        self.assertEqual(normalize_breakout_universe("sp500"), "SP500")
        self.assertEqual(
            normalize_breakout_universe("WATCHLIST:my-list"),
            "watchlist:my-list",
        )

    def test_unknown_universe_is_an_application_error_not_http_error(self):
        with self.assertRaises(UnknownBreakoutUniverseError):
            resolve_breakout_universe(
                "NOT_A_UNIVERSE",
                enabled_universes=("SP500", "MAG7"),
            )


if __name__ == "__main__":
    unittest.main()
