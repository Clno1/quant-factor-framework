from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

from src.data.foundation import DataFoundationError
from src.data.access import enqueue_market_data_request
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


def test_frame_read_uses_one_snapshot_during_concurrent_replacement(tmp_path, monkeypatch):
    reader = AppDatabase(tmp_path / "app.sqlite3")
    writer = AppDatabase(reader.path)
    old = pd.DataFrame([{"ticker": "AAA"}])
    new = pd.DataFrame([{"ticker": "AAA"}, {"ticker": "BBB"}])
    writer.put_frame("paper", "one", "positions", old)
    reader.initialize()
    connect = reader._connect
    class Connection:
        def __init__(self):
            self.inner = connect()
        def execute(self, sql, *args):
            cursor = self.inner.execute(sql, *args)
            if "SELECT columns_json" in sql:
                class Cursor:
                    def fetchone(self):
                        row = cursor.fetchone()
                        writer.put_frame("paper", "one", "positions", new)
                        return row
                return Cursor()
            return cursor
        def close(self):
            self.inner.close()
    monkeypatch.setattr(reader, "_connect", Connection)
    pd.testing.assert_frame_equal(reader.get_frame("paper", "one", "positions"), old)
    pd.testing.assert_frame_equal(writer.get_frame("paper", "one", "positions"), new)


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
    writer.catalog.latest_version.return_value = None
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


def test_request_worker_extends_membership_to_existing_history_start(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    request = database.enqueue_data_request(
        data_universe="WATCHLIST_TEST",
        payload={
            "universe_records": [{"ticker": "AAA"}],
            "tickers": ["AAA"],
            "initial_start": "2020-07-07",
        },
        consumer_kind="backtest",
        consumer_id="task-one",
    )
    result = Mock()
    result.to_dict.return_value = {"version_id": "version-two"}
    result.version.version_id = "version-two"
    writer = Mock()
    writer.catalog.latest_version.return_value = SimpleNamespace(
        min_date="2020-07-06"
    )
    writer.update_universe.return_value = result

    with patch("src.data.request_worker.MarketDataReader") as reader_class:
        reader_class.return_value.verify_version.return_value = {
            "price_semantics": {"schema_version": 1}
        }
        processed = process_pending_data_requests(
            limit=1,
            database=database,
            writer=writer,
        )

    assert processed[0].status == DATA_REQUEST_SUCCESS
    membership = writer.update_universe.call_args.kwargs["membership_frame"]
    assert membership["date"].min() == pd.Timestamp("2020-07-06")
    assert writer.update_universe.call_args.kwargs["full_rebuild"] is False


def test_request_worker_full_rebuilds_only_legacy_price_semantics(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    request = database.enqueue_data_request(
        data_universe="WATCHLIST_TEST",
        payload={
            "universe_records": [{"ticker": "AAA"}],
            "tickers": ["AAA"],
            "initial_start": "2020-07-07",
        },
        consumer_kind="backtest",
        consumer_id="task-one",
    )
    result = Mock()
    result.to_dict.return_value = {"version_id": "version-three"}
    result.version.version_id = "version-three"
    writer = Mock()
    writer.catalog.latest_version.return_value = SimpleNamespace(
        min_date="2020-07-06"
    )
    writer.update_universe.return_value = result

    with patch("src.data.request_worker.MarketDataReader") as reader_class:
        reader_class.return_value.verify_version.side_effect = DataFoundationError(
            "version predates the authenticated price-semantics contract"
        )
        processed = process_pending_data_requests(
            limit=1,
            database=database,
            writer=writer,
        )

    assert processed[0].status == DATA_REQUEST_SUCCESS
    assert writer.update_universe.call_args.kwargs["full_rebuild"] is True


def test_request_worker_full_rebuilds_only_confirmed_semantic_drift(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    request = database.enqueue_data_request(
        data_universe="WATCHLIST_TEST",
        payload={
            "universe_records": [{"ticker": "AAA"}],
            "tickers": ["AAA"],
            "initial_start": "2020-01-01",
        },
        consumer_kind="paper",
        consumer_id="paper-one",
    )
    result = Mock()
    result.to_dict.return_value = {"version_id": "version-rebuilt"}
    result.version.version_id = "version-rebuilt"
    writer = Mock()
    writer.catalog.latest_version.return_value = None
    writer.update_universe.side_effect = [
        DataFoundationError(
            "AAA: non-uniform volume revision in overlap window; "
            "run a full rebuild"
        ),
        result,
    ]

    processed = process_pending_data_requests(
        limit=1,
        database=database,
        writer=writer,
    )

    assert processed[0].status == DATA_REQUEST_SUCCESS
    assert writer.update_universe.call_count == 2
    first, second = writer.update_universe.call_args_list
    assert first.kwargs["full_rebuild"] is False
    assert second.kwargs["full_rebuild"] is True


def test_request_worker_does_not_rebuild_over_integrity_failure(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    request = database.enqueue_data_request(
        data_universe="WATCHLIST_TEST",
        payload={
            "universe_records": [{"ticker": "AAA"}],
            "tickers": ["AAA"],
            "initial_start": "2020-07-07",
        },
        consumer_kind="backtest",
        consumer_id="task-one",
    )
    writer = Mock()
    writer.catalog.latest_version.return_value = SimpleNamespace(
        min_date="2020-07-06"
    )

    with patch("src.data.request_worker.MarketDataReader") as reader_class:
        reader_class.return_value.verify_version.side_effect = DataFoundationError(
            "manifest checksum mismatch"
        )
        processed = process_pending_data_requests(
            limit=1,
            database=database,
            writer=writer,
        )

    assert processed[0].status != DATA_REQUEST_SUCCESS
    writer.update_universe.assert_not_called()


def test_request_worker_does_not_rebuild_provider_failures(tmp_path):
    database = AppDatabase(tmp_path / "app.sqlite3")
    request = database.enqueue_data_request(
        data_universe="WATCHLIST_TEST",
        payload={
            "universe_records": [{"ticker": "AAA"}],
            "tickers": ["AAA"],
            "initial_start": "2020-01-01",
        },
        consumer_kind="paper",
        consumer_id="paper-one",
    )
    writer = Mock()
    writer.catalog.latest_version.return_value = None
    writer.update_universe.side_effect = DataFoundationError(
        "FMP request timed out after 6 attempts"
    )

    processed = process_pending_data_requests(
        limit=1,
        database=database,
        writer=writer,
    )

    assert processed[0].status != DATA_REQUEST_SUCCESS
    assert writer.update_universe.call_count == 1


def test_market_data_request_v3_does_not_deduplicate_to_legacy_success():
    expected = object()
    universe = pd.DataFrame([{"ticker": "AAA", "name": "Alpha"}])

    with patch("src.data.access.app_database") as database_factory:
        database_factory.return_value.enqueue_data_request.return_value = expected
        actual = enqueue_market_data_request(
            data_universe="WATCHLIST_TEST",
            universe_frame=universe,
            start="2025-01-01",
            end="2025-12-31",
            consumer_kind="backtest",
            consumer_id="task-one",
        )

    assert actual is expected
    payload = database_factory.return_value.enqueue_data_request.call_args.kwargs[
        "payload"
    ]
    assert payload["schema_version"] == 3
