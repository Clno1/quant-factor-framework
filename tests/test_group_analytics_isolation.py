from __future__ import annotations

import ast
import os
from pathlib import Path
import unittest
from unittest import mock

from src.config import CONFIG, PROJECT_ROOT
from src.group_analytics.settings import load_group_analytics_settings


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _registered_paths(app) -> set[str]:
    pending = list(app.routes)
    paths: set[str] = set()
    while pending:
        route = pending.pop()
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            pending.extend(original_router.routes)
    return paths


class GroupAnalyticsIsolationTests(unittest.TestCase):
    def test_default_config_disables_group_analytics(self):
        self.assertFalse(bool(CONFIG.group_analytics.enabled))
        self.assertFalse(bool(CONFIG.group_analytics.web_enabled))

    def test_string_false_flags_never_enable_writer_or_web(self):
        settings = load_group_analytics_settings(
            {
                "group_analytics": {
                    "enabled": "false",
                    "web_enabled": "false",
                    "classification": {"include_etfs": "false"},
                    "inputs": {"require_benchmark": "false"},
                },
                "storage": {"output_dir": "outputs"},
            }
        )
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.web_enabled)
        self.assertFalse(settings.classification.include_etfs)
        self.assertFalse(settings.inputs.require_benchmark)

        from src.webapp.app import _strict_config_flag

        self.assertFalse(_strict_config_flag("false"))
        with self.assertRaises(ValueError):
            _strict_config_flag("definitely")

    def test_disabled_app_does_not_register_group_routes(self):
        from src.webapp.app import create_app

        paths = _registered_paths(create_app())
        self.assertNotIn("/group-analytics", paths)
        self.assertFalse(any(path.startswith("/api/group-analytics") for path in paths))

    def test_web_routes_require_both_independent_environment_flags(self):
        from src.webapp.app import create_app

        with mock.patch.dict(
            os.environ,
            {
                "GROUP_ANALYTICS_ENABLED": "false",
                "GROUP_ANALYTICS_WEB_ENABLED": "true",
            },
        ):
            writer_off_paths = _registered_paths(create_app())
        self.assertNotIn("/group-analytics", writer_off_paths)

        with mock.patch.dict(
            os.environ,
            {
                "GROUP_ANALYTICS_ENABLED": "true",
                "GROUP_ANALYTICS_WEB_ENABLED": "true",
            },
        ):
            enabled_paths = _registered_paths(create_app())
        self.assertIn("/group-analytics", enabled_paths)
        self.assertIn("/api/group-analytics/heat", enabled_paths)

    def test_core_domains_do_not_import_group_analytics(self):
        core_domains = (
            "factors", "preprocessing", "analysis", "backtest", "papertrading",
            "strategies", "execution", "alerts", "breakouts", "watchlists",
            "data", "utils",
        )
        violations: list[str] = []
        for domain in core_domains:
            for path in (PROJECT_ROOT / "src" / domain).rglob("*.py"):
                if any(name == "src.group_analytics" or name.startswith("src.group_analytics.")
                       for name in _imports(path)):
                    violations.append(str(path.relative_to(PROJECT_ROOT)))
        self.assertEqual(violations, [])

    def test_group_domain_does_not_import_application_domains(self):
        forbidden = (
            "src.webapp", "src.factors", "src.preprocessing", "src.analysis",
            "src.backtest", "src.papertrading", "src.strategies", "src.execution",
            "src.alerts", "src.breakouts", "src.watchlists",
        )
        violations: list[str] = []
        for path in (PROJECT_ROOT / "src" / "group_analytics").rglob("*.py"):
            for imported in _imports(path):
                if any(imported == prefix or imported.startswith(prefix + ".") for prefix in forbidden):
                    violations.append(f"{path.relative_to(PROJECT_ROOT)} -> {imported}")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
