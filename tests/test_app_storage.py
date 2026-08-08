from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pandas as pd

from src.data.request_worker import process_pending_data_requests
from src.storage import (
    DATA_REQUEST_FAILED,
    DATA_REQUEST_PENDING,
    DATA_REQUEST_RUNNING,
    DATA_REQUEST_SUCCESS,
    AppDatabase,
)


def test_sqlite_records_and_frames_round_trip(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    payload = {"id": "one", "name": "Example", "nested": {"enabled": True}}
    database.put_record("strategy", "one", payload, {"id": "one", "name": "Example"})
    assert database.get_record("strategy", "one") == payload
    assert database.list_summaries("strategy") == [
        {"id": "one", "name": "Example"}
    ]

    frame = pd.DataFrame(
        [{"ticker": "AAA", "quantity": 10.0}, {"ticker": "BBB", "quantity": 5.0}]
    )
    database.put_frame("paper_account", "account-one", "positions", frame)
    pd.testing.assert_frame_equal(
        database.get_frame("paper_account", "account-one", "positions"),
        frame,
        check_dtype=False,
    )
    assert database.verify_integrity() == {
        "path": str(tmp_path / "app.sqlite3"),
        "sqlite_integrity": ["ok"],
        "records": {"strategy": 1},
        "frames": 1,
        "data_requests": 0,
        "issues": [],
        "passed": True,
    }


def test_data_request_retries_then_becomes_terminal(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    request = database.enqueue_data_request(
        data_universe="WATCHLIST_TEST",
        payload={"tickers": ["AAA"]},
        consumer_kind="backtest",
        consumer_id="task-one",
    )
    assert request.status == DATA_REQUEST_PENDING

    first = database.claim_data_requests(limit=1)[0]
    assert first.status == DATA_REQUEST_RUNNING
    assert first.attempts == 1
    assert (
        database.retry_or_fail_data_request(
            first.request_id,
            error="temporary",
            max_attempts=2,
        )
        == DATA_REQUEST_PENDING
    )

    second = database.claim_data_requests(limit=1)[0]
    assert second.attempts == 2
    assert (
        database.retry_or_fail_data_request(
            second.request_id,
            error="permanent",
            max_attempts=2,
        )
        == DATA_REQUEST_FAILED
    )
    terminal = database.get_data_request(second.request_id)
    assert terminal is not None
    assert terminal.status == DATA_REQUEST_FAILED
    assert terminal.error == "permanent"


def test_stale_running_data_request_is_requeued(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    request = database.enqueue_data_request(
        data_universe="WATCHLIST_TEST",
        payload={"tickers": ["AAA"]},
        consumer_kind="paper_account",
        consumer_id="account-one",
    )
    database.claim_data_requests(limit=1)
    stale = (
        datetime.now(timezone.utc) - timedelta(hours=2)
    ).isoformat(timespec="seconds")
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            UPDATE data_requests
            SET started_at = ?, updated_at = ?
            WHERE request_id = ?
            """,
            [stale, stale, request.request_id],
        )

    recovered = database.recover_stale_data_requests(stale_after_seconds=60)
    assert recovered == {"requeued": 1, "failed": 0}
    current = database.get_data_request(request.request_id)
    assert current is not None
    assert current.status == DATA_REQUEST_PENDING


def test_pending_request_worker_publishes_and_finishes_transaction(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    request = database.enqueue_data_request(
        data_universe="WATCHLIST_TEST",
        payload={
            "universe_records": [{"ticker": "AAA"}],
            "tickers": ["AAA"],
            "initial_start": "2025-01-01",
        },
        consumer_kind="backtest",
        consumer_id="task-one",
    )
    result = Mock()
    result.to_dict.return_value = {"version_id": "version-one"}
    result.version.version_id = "version-one"
    writer = Mock()
    writer.update_universe.return_value = result

    processed = process_pending_data_requests(
        limit=1,
        database=database,
        writer=writer,
    )

    assert len(processed) == 1
    assert processed[0].status == DATA_REQUEST_SUCCESS
    current = database.get_data_request(request.request_id)
    assert current is not None
    assert current.status == DATA_REQUEST_SUCCESS
    assert current.attempts == 1
    assert writer.update_universe.call_args.kwargs[
        "derive_membership_from_bars"
    ] is True
