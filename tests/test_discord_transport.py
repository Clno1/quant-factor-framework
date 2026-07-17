from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlparse

import requests

from src.alerts.discord import (
    DiscordDeliveryError,
    DiscordNotifier,
    build_discord_payload,
    validate_discord_payload,
)


WEBHOOK = "https://discord.com/api/webhooks/123/super-secret-token"


def _response(status: int, data=None, *, headers=None, text: str = "") -> Mock:
    response = Mock(status_code=status, headers=headers or {}, text=text)
    if isinstance(data, Exception):
        response.json.side_effect = data
    else:
        response.json.return_value = data
    return response


class DiscordTransportTests(unittest.TestCase):
    def test_legacy_hourly_payload_respects_combined_embed_budget(self) -> None:
        snapshot = {
            "market_hours": {"isMarketOpen": True},
            "broad_count": 20,
            "strict_count": 20,
            "pending_upgrade_count": 20,
            "quote_time": "2026-07-16T15:00:00Z",
            "generated_at": "2026-07-16T15:00:01Z",
        }
        rows = [
            {
                "ticker": f"T{index:02d}",
                "name": "N" * 80,
                "signal_type": "READY",
                "score": 90,
                "close": 100,
                "pivot": 101,
                "pivot_distance": -1,
                "return_20d": 25,
                "adr_20d": 7,
                "dollar_volume": 50e6,
                "avg_dollar_volume_20d": 40e6,
                "intraday_trigger": "I" * 50,
            }
            for index in range(20)
        ]
        payload = build_discord_payload(
            snapshot,
            rows,
            dashboard_base_url="https://example.com/diagnostics/" + "d" * 80,
        )
        validate_discord_payload(payload)
        embed = payload["embeds"][0]
        total = (
            len(embed["title"])
            + len(embed["description"])
            + len(embed["footer"]["text"])
            + sum(
                len(field["name"]) + len(field["value"])
                for field in embed["fields"]
            )
        )
        self.assertLessEqual(total, 5_500)
        self.assertEqual(len(embed["fields"]), 20)

    @patch("src.alerts.discord.requests.post")
    def test_success_forces_wait_and_injects_safe_mentions_without_mutating_input(
        self, post_mock: Mock
    ) -> None:
        post_mock.return_value = _response(200, {"id": "message-1"})
        payload = {"content": "test"}
        notifier = DiscordNotifier(WEBHOOK + "?thread_id=456&wait=false")

        result = notifier.send(payload)

        self.assertEqual(result, {"status": 200, "message_id": "message-1"})
        request_url = post_mock.call_args.args[0]
        query = parse_qs(urlparse(request_url).query)
        self.assertEqual(query["wait"], ["true"])
        self.assertEqual(query["thread_id"], ["456"])
        self.assertFalse(post_mock.call_args.kwargs["allow_redirects"])
        self.assertEqual(post_mock.call_args.kwargs["json"]["allowed_mentions"], {"parse": []})
        self.assertNotIn("allowed_mentions", payload)

    @patch("src.alerts.discord.requests.post")
    def test_network_exception_is_sanitized_and_marked_uncertain(self, post_mock: Mock) -> None:
        post_mock.side_effect = requests.ReadTimeout(f"request failed for {WEBHOOK}")

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(WEBHOOK).send({"content": "test"})

        error = caught.exception
        self.assertTrue(error.uncertain)
        self.assertFalse(error.retryable)
        self.assertIsNone(error.status_code)
        self.assertEqual(error.reason, "response_timeout")
        self.assertNotIn(WEBHOOK, str(error))
        self.assertNotIn("super-secret-token", str(error))

    @patch("src.alerts.discord.requests.post")
    def test_connect_timeout_is_safe_retry_classification(self, post_mock: Mock) -> None:
        post_mock.side_effect = requests.ConnectTimeout(f"connect failed for {WEBHOOK}")

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(WEBHOOK).send({"content": "test"})

        error = caught.exception
        self.assertFalse(error.uncertain)
        self.assertTrue(error.retryable)
        self.assertEqual(error.reason, "connect_timeout")
        self.assertNotIn("super-secret-token", str(error))

    @patch("src.alerts.discord.requests.post")
    def test_two_xx_requires_json_message_id(self, post_mock: Mock) -> None:
        post_mock.return_value = _response(204, ValueError("not json"))

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(WEBHOOK).send({"content": "test"})

        error = caught.exception
        self.assertEqual(error.status_code, 204)
        self.assertTrue(error.uncertain)
        self.assertFalse(error.retryable)
        self.assertEqual(error.reason, "missing_message_id")

    @patch("src.alerts.discord.requests.post")
    def test_rate_limit_header_is_slept_then_successfully_retried(self, post_mock: Mock) -> None:
        post_mock.side_effect = [
            _response(429, {"retry_after": 9.0}, headers={"Retry-After": "0.25"}),
            _response(200, {"id": "message-2"}),
        ]
        sleep = Mock()

        result = DiscordNotifier(WEBHOOK, sleep=sleep).send({"content": "test"})

        self.assertEqual(result["message_id"], "message-2")
        sleep.assert_called_once_with(0.25)
        self.assertEqual(post_mock.call_count, 2)

    @patch("src.alerts.discord.requests.post")
    def test_rate_limit_json_float_is_exposed_after_bounded_retry(self, post_mock: Mock) -> None:
        post_mock.side_effect = [
            _response(429, {"retry_after": 0.125}),
            _response(429, {"retry_after": 0.5}),
        ]
        sleep = Mock()

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(
                WEBHOOK,
                max_rate_limit_retries=1,
                sleep=sleep,
            ).send({"content": "test"})

        error = caught.exception
        self.assertEqual(post_mock.call_count, 2)
        self.assertEqual(sleep.call_args_list, [call(0.125)])
        self.assertEqual(error.status_code, 429)
        self.assertEqual(error.retry_after, 0.5)
        self.assertFalse(error.uncertain)
        self.assertTrue(error.retryable)
        self.assertEqual(error.reason, "rate_limited")

    @patch("src.alerts.discord.requests.post")
    def test_permanent_http_errors_are_not_retried_or_leaked(self, post_mock: Mock) -> None:
        for status_code in (400, 401, 403, 404):
            with self.subTest(status_code=status_code):
                post_mock.reset_mock()
                post_mock.return_value = _response(
                    status_code,
                    {"message": f"leaked {WEBHOOK}"},
                    text=f"leaked {WEBHOOK}",
                )

                with self.assertRaises(DiscordDeliveryError) as caught:
                    DiscordNotifier(WEBHOOK).send({"content": "test"})

                error = caught.exception
                self.assertEqual(post_mock.call_count, 1)
                self.assertEqual(error.status_code, status_code)
                self.assertFalse(error.uncertain)
                self.assertFalse(error.retryable)
                self.assertNotIn("super-secret-token", str(error))

    @patch("src.alerts.discord.requests.post")
    def test_server_error_is_uncertain_to_preserve_at_most_once(self, post_mock: Mock) -> None:
        post_mock.return_value = _response(503, {"message": "unavailable"})

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(WEBHOOK, max_rate_limit_retries=5).send({"content": "test"})

        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertTrue(caught.exception.uncertain)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.reason, "server_error_unknown")

    @patch("src.alerts.discord.requests.post")
    def test_redirect_is_not_followed_or_reposted(self, post_mock: Mock) -> None:
        post_mock.return_value = _response(307, {"message": "redirect"})

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(WEBHOOK).send({"content": "test"})

        self.assertEqual(post_mock.call_count, 1)
        self.assertEqual(caught.exception.status_code, 307)
        self.assertFalse(caught.exception.uncertain)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.reason, "http_error")

    @patch("src.alerts.discord.requests.post")
    def test_rate_limit_retry_never_crosses_delivery_deadline(self, post_mock: Mock) -> None:
        post_mock.return_value = _response(429, {"retry_after": 45.0})
        sleep = Mock()
        guard = Mock(side_effect=lambda delay: delay < 10.0)

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(
                WEBHOOK,
                sleep=sleep,
                request_guard=guard,
            ).send({"content": "test"})

        self.assertEqual(caught.exception.reason, "delivery_window_closed")
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(post_mock.call_count, 1)
        sleep.assert_not_called()

    @patch("src.alerts.discord.requests.post")
    def test_unsafe_allowed_mentions_fail_before_network(self, post_mock: Mock) -> None:
        payload = {
            "content": "@everyone",
            "allowed_mentions": {"parse": ["everyone"]},
        }

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(WEBHOOK).send(payload)

        self.assertFalse(caught.exception.uncertain)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.reason, "invalid_payload")
        post_mock.assert_not_called()

    @patch("src.alerts.discord.requests.post")
    def test_unicode_digits_are_not_valid_discord_ids(self, post_mock: Mock) -> None:
        payload = {
            "content": "role",
            "allowed_mentions": {"parse": [], "roles": ["١٢٣"]},
        }

        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(WEBHOOK).send(payload)

        self.assertEqual(caught.exception.reason, "invalid_payload")
        post_mock.assert_not_called()

    @patch("src.alerts.discord.requests.post")
    def test_content_and_embed_limits_fail_before_network(self, post_mock: Mock) -> None:
        invalid_payloads = {
            "content": {"content": "x" * 2_001},
            "embed_count": {"embeds": [{} for _ in range(11)]},
            "title": {"embeds": [{"title": "x" * 257}]},
            "description": {"embeds": [{"description": "x" * 4_097}]},
            "field_count": {
                "embeds": [{"fields": [{"name": "n", "value": "v"} for _ in range(26)]}]
            },
            "field_name": {
                "embeds": [{"fields": [{"name": "n" * 257, "value": "v"}]}]
            },
            "field_value": {
                "embeds": [{"fields": [{"name": "n", "value": "v" * 1_025}]}]
            },
            "footer": {"embeds": [{"footer": {"text": "x" * 2_049}}]},
            "author": {"embeds": [{"author": {"name": "x" * 257}}]},
            "combined": {
                "embeds": [
                    {"description": "x" * 3_000},
                    {"description": "x" * 3_001},
                ]
            },
        }

        for name, payload in invalid_payloads.items():
            with self.subTest(limit=name):
                with self.assertRaises(DiscordDeliveryError) as caught:
                    DiscordNotifier(WEBHOOK).send(payload)
                self.assertEqual(caught.exception.reason, "invalid_payload")
        post_mock.assert_not_called()

    @patch("src.alerts.discord.requests.post")
    def test_non_json_payload_fails_deterministically_before_network(self, post_mock: Mock) -> None:
        with self.assertRaises(DiscordDeliveryError) as caught:
            DiscordNotifier(WEBHOOK).send({"content": "test", "custom": object()})

        self.assertFalse(caught.exception.uncertain)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.reason, "invalid_payload")
        post_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
