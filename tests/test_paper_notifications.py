from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts.configure_paper_discord import _assert_independent_webhook
from src.alerts.discord import DiscordDeliveryError, validate_discord_payload
from src.papertrading.notification_state import (
    DELIVERY_BASELINED,
    DELIVERY_PENDING,
    DELIVERY_SENT,
    DELIVERY_UNKNOWN,
    KIND_FILL,
    PaperNotificationState,
)
from src.papertrading.notifications import (
    PaperNotificationService,
    PaperNotificationSettings,
    build_daily_discord_payload,
    build_fill_discord_payload,
)


def _settings(path: Path, *, enabled: bool = False) -> PaperNotificationSettings:
    return PaperNotificationSettings(
        state_path=path,
        webhook_url=(
            "https://discord.com/api/webhooks/123/test-token" if enabled else ""
        ),
        delivery_enabled=enabled,
        dashboard_base_url="http://localhost:18823",
        max_attempts=3,
        batch_limit=25,
    )


def _fill(fill_id: str = "fill-1") -> dict:
    return {
        "fill_id": fill_id,
        "order_id": "order-1",
        "account_id": "account-1",
        "ticker": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "raw_open_price": 100.0,
        "fill_price": 100.02,
        "notional": 1_000.2,
        "slippage_bps": 2.0,
        "slippage_cost": 0.2,
        "fee_model": "ibkr_us_pro_fixed",
        "slippage_model": "volume_share",
        "broker_commission": 1.0,
        "sec_fee": 0.0,
        "finra_taf": 0.0,
        "finra_cat": 0.0,
        "fee": 1.01,
        "decision_date": "2026-08-27",
        "fill_date": "2026-08-28",
        "filled_at": "2026-08-30T16:04:06+08:00",
    }


def test_fill_payload_contains_execution_cost_contract():
    payload = build_fill_discord_payload(
        account={"id": "account-1", "name": "测试模拟盘"},
        fill=_fill(),
        dataset_version_id="dataset-version-123",
        dashboard_base_url="http://localhost:18823",
    )
    validate_discord_payload(payload)
    rendered = str(payload)
    assert "next_open" in rendered
    assert "ibkr_us_pro_fixed" in rendered
    assert "volume_share" in rendered
    assert "2026-08-27" in rendered
    assert "2026-08-28" in rendered


def test_daily_summary_retry_keeps_the_first_frozen_payload(tmp_path, monkeypatch):
    import src.papertrading.notifications as notifications
    from src.papertrading.notification_state import KIND_DAILY_SUMMARY

    monkeypatch.setattr(notifications, "list_accounts", lambda: [])
    state = PaperNotificationState(tmp_path / "outbox.sqlite3")
    service = PaperNotificationService(_settings(state.path), state=state)
    assert service.stage_daily_summary(target_session="2026-01-05")
    # A repeat builds a different wall-clock timestamp but cannot rewrite or
    # prevent delivery of the frozen logical daily message.
    assert not service.stage_daily_summary(target_session="2026-01-05")
    claim = state.claim_next(kinds={KIND_DAILY_SUMMARY}, max_attempts=3)
    assert claim is not None
    assert claim.delivery_id == "paper-daily:2026-01-05"
    assert not service.stage_daily_summary(target_session="2026-01-05")


def test_outbox_is_immutable_and_exactly_once(tmp_path):
    state = PaperNotificationState(tmp_path / "state.sqlite3")
    payload = {"content": "one", "allowed_mentions": {"parse": []}}
    assert state.stage(
        delivery_id="paper-fill:fill-1",
        kind=KIND_FILL,
        account_id="account-1",
        target_session="2026-08-28",
        source_id="fill-1",
        payload=payload,
    )
    assert not state.stage(
        delivery_id="paper-fill:fill-1",
        kind=KIND_FILL,
        account_id="account-1",
        target_session="2026-08-28",
        source_id="fill-1",
        payload=payload,
    )
    with pytest.raises(RuntimeError, match="different immutable content"):
        state.stage(
            delivery_id="paper-fill:fill-1",
            kind=KIND_FILL,
            account_id="account-1",
            target_session="2026-08-28",
            source_id="fill-1",
            payload={"content": "changed", "allowed_mentions": {"parse": []}},
        )
    claim = state.claim_next(kinds={KIND_FILL}, max_attempts=3)
    assert claim is not None
    state.mark_sent(claim, message_id="discord-1")
    assert state.claim_next(kinds={KIND_FILL}, max_attempts=3) is None
    assert state.status()["counts"] == {f"{KIND_FILL}:{DELIVERY_SENT}": 1}
    assert len(state.status()["recent"]) == 1
    assert "recent" not in state.status(include_recent=False)


def test_interrupted_sending_becomes_unknown_and_is_not_retried(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = PaperNotificationState(path)
    state.stage(
        delivery_id="paper-fill:fill-1",
        kind=KIND_FILL,
        account_id="account-1",
        target_session="2026-08-28",
        source_id="fill-1",
        payload={"content": "one", "allowed_mentions": {"parse": []}},
    )
    assert state.claim_next(kinds={KIND_FILL}, max_attempts=3) is not None
    restarted = PaperNotificationState(path)
    assert restarted.claim_next(kinds={KIND_FILL}, max_attempts=3) is None
    assert restarted.status()["counts"] == {
        f"{KIND_FILL}:{DELIVERY_UNKNOWN}": 1
    }


def test_reconciliation_baselines_history_then_stages_only_new_fill(
    monkeypatch,
    tmp_path,
):
    fills = [_fill()]
    monkeypatch.setattr(
        "src.papertrading.notifications.list_accounts",
        lambda: [{"id": "account-1", "status": "active"}],
    )
    monkeypatch.setattr(
        "src.papertrading.notifications.load_account",
        lambda account_id: {"id": account_id, "name": "测试模拟盘"},
    )

    def load_table(account_id: str, name: str) -> pd.DataFrame:
        if name == "fills":
            return pd.DataFrame(fills)
        return pd.DataFrame()

    monkeypatch.setattr("src.papertrading.notifications.load_table", load_table)
    service = PaperNotificationService(_settings(tmp_path / "state.sqlite3"))
    assert service.reconcile_fills(baseline=True) == 1
    assert service.state.status()["counts"] == {
        f"{KIND_FILL}:{DELIVERY_BASELINED}": 1
    }
    fills.append(_fill("fill-2"))
    assert service.reconcile_fills() == 1
    assert service.state.status()["counts"] == {
        f"{KIND_FILL}:{DELIVERY_BASELINED}": 1,
        f"{KIND_FILL}:{DELIVERY_PENDING}": 1,
    }


def test_retryable_transport_failure_remains_bounded(tmp_path):
    class FailingNotifier:
        def send(self, payload):
            raise DiscordDeliveryError(
                "safe connect timeout",
                uncertain=False,
                retryable=True,
                reason="connect_timeout",
            )

    service = PaperNotificationService(
        _settings(tmp_path / "state.sqlite3", enabled=True),
        notifier=FailingNotifier(),
    )
    service.state.stage(
        delivery_id="paper-fill:fill-1",
        kind=KIND_FILL,
        account_id="account-1",
        target_session="2026-08-28",
        source_id="fill-1",
        payload={"content": "one", "allowed_mentions": {"parse": []}},
    )
    assert service.drain(kinds={KIND_FILL}) == {
        "sent": 0,
        "failed": 1,
        "unknown": 0,
    }
    assert service.drain(kinds={KIND_FILL}) == {
        "sent": 0,
        "failed": 1,
        "unknown": 0,
    }
    assert service.drain(kinds={KIND_FILL}) == {
        "sent": 0,
        "failed": 1,
        "unknown": 0,
    }
    assert service.state.status()["counts"] == {f"{KIND_FILL}:FAILED": 1}


def test_daily_payload_summarizes_equity_fills_and_costs(monkeypatch):
    account = {
        "id": "account-1",
        "name": "测试模拟盘",
        "initial_cash": 1_000.0,
        "cash": 100.0,
        "last_equity": 1_050.0,
        "last_mark_date": "2026-08-28",
        "last_error": None,
    }

    def load_table(account_id: str, name: str) -> pd.DataFrame:
        frames = {
            "equity_curve": pd.DataFrame([
                {"date": "2026-08-27", "equity": 1_020.0},
                {"date": "2026-08-28", "equity": 1_050.0},
            ]),
            "fills": pd.DataFrame([_fill()]),
            "orders": pd.DataFrame([{"status": "filled"}]),
            "positions": pd.DataFrame([{"ticker": "AAPL"}]),
            "runs": pd.DataFrame([{
                "mark_date": "2026-08-28",
                "run_at": "2026-08-30T16:04:06+08:00",
                "dataset_version_id": "dataset-version-123",
            }]),
        }
        return frames.get(name, pd.DataFrame())

    monkeypatch.setattr("src.papertrading.notifications.load_table", load_table)
    payload = build_daily_discord_payload(
        accounts=[account],
        target_session="2026-08-28",
        generated_at="2026-08-30T03:00:00+00:00",
    )
    validate_discord_payload(payload)
    rendered = str(payload)
    assert "+30.00 美元" in rendered
    assert "1/1" in rendered
    assert "dataset-vers" in rendered


def test_paper_webhook_cannot_reuse_an_existing_business_channel(tmp_path):
    env_file = tmp_path / "premarket.env"
    webhook = "https://discord.com/api/webhooks/123/shared-secret"
    env_file.write_text(
        f"DISCORD_SECTOR_ROTATION_WEBHOOK_URL={webhook}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="独立 Webhook"):
        _assert_independent_webhook(webhook, env_file=env_file)
