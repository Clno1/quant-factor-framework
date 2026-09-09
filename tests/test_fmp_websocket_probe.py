from __future__ import annotations

import unittest

from scripts.probe_fmp_websocket import (
    MAX_PROBE_SYMBOLS,
    control_state,
    parse_message,
    parse_symbols,
)


class FmpWebSocketProbeTests(unittest.TestCase):
    def test_symbols_are_lowercase_deduplicated_and_bounded(self):
        self.assertEqual(
            parse_symbols("AAPL, aeva,AAPL,BRK.B"),
            ("aapl", "aeva", "brk.b"),
        )
        with self.assertRaises(ValueError):
            parse_symbols("")
        with self.assertRaises(ValueError):
            parse_symbols(",".join(f"s{index}" for index in range(MAX_PROBE_SYMBOLS + 1)))
        with self.assertRaises(ValueError):
            parse_symbols("aapl,$secret")

    def test_control_responses_are_classified_without_exact_schema_dependency(self):
        self.assertEqual(
            control_state({"event": "login", "status": 200}),
            "authenticated",
        )
        self.assertEqual(
            control_state({"message": "Unauthorized"}),
            "unauthorized",
        )
        self.assertEqual(control_state({"event": "connected"}), "unknown")

    def test_non_json_messages_are_not_echoed(self):
        self.assertEqual(parse_message("not-json-and-not-a-secret"), {"_non_json": True})


if __name__ == "__main__":
    unittest.main()
