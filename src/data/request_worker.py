"""Worker for centralized custom-universe market-data requests."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.data.foundation import MarketDataWriter
from src.config import CONFIG
from src.storage import (
    DATA_REQUEST_SUCCESS,
    AppDatabase,
    DataRequest,
    app_database,
)
from src.utils.logger import get_logger


log = get_logger(__name__)


@dataclass(frozen=True)
class RequestProcessingResult:
    request_id: str
    status: str
    payload: dict[str, Any]


def _membership_for_request(
    request: DataRequest,
) -> pd.DataFrame:
    payload = request.payload
    baseline = pd.Timestamp(payload["initial_start"]).normalize()
    return pd.DataFrame(
        {
            "date": baseline,
            "ticker": list(payload["tickers"]),
            "active": True,
        }
    )


def process_data_request(
    request: DataRequest,
    *,
    database: AppDatabase | None = None,
    writer: MarketDataWriter | None = None,
) -> RequestProcessingResult:
    database = database or app_database()
    writer = writer or MarketDataWriter()
    try:
        payload = request.payload
        universe_frame = pd.DataFrame(payload.get("universe_records") or [])
        if universe_frame.empty or "ticker" not in universe_frame.columns:
            raise ValueError("Data request contains no universe records")
        membership = _membership_for_request(request)
        result = writer.update_universe(
            request.data_universe,
            target_session=None,
            # A request exists because the current version failed preflight.
            force=True,
            universe_frame=universe_frame,
            initial_start=payload["initial_start"],
            membership_frame=membership,
            membership_source=f"sqlite_data_request:{request.request_id}",
            derive_membership_from_bars=True,
            min_latest_coverage=1.0,
        )
        result_payload = result.to_dict()
        database.finish_data_request(
            request.request_id,
            status=DATA_REQUEST_SUCCESS,
            result=result_payload,
        )
        log.info(
            "Data request completed: request=%s universe=%s version=%s",
            request.request_id,
            request.data_universe,
            result.version.version_id if result.version is not None else None,
        )
        return RequestProcessingResult(
            request_id=request.request_id,
            status=DATA_REQUEST_SUCCESS,
            payload=result_payload,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        status = database.retry_or_fail_data_request(
            request.request_id,
            error=error,
        )
        log.exception(
            "Data request attempt failed: request=%s universe=%s next_status=%s",
            request.request_id,
            request.data_universe,
            status,
        )
        return RequestProcessingResult(
            request_id=request.request_id,
            status=status,
            payload={"error": error},
        )


def process_pending_data_requests(
    *,
    limit: int = 10,
    database: AppDatabase | None = None,
    writer: MarketDataWriter | None = None,
) -> list[RequestProcessingResult]:
    database = database or app_database()
    writer = writer or MarketDataWriter()
    try:
        stale_seconds = int(CONFIG.data.foundation.request_stale_minutes) * 60
    except (AttributeError, KeyError, TypeError, ValueError):
        stale_seconds = 1800
    recovery = database.recover_stale_data_requests(
        stale_after_seconds=stale_seconds,
    )
    if recovery["requeued"] or recovery["failed"]:
        log.warning("Recovered stale data requests: %s", recovery)
    requests = database.claim_data_requests(limit=limit)
    reader = MarketDataReader(catalog=writer.catalog)
    return [
        process_data_request(
            request,
            database=database,
            writer=writer,
            reader=reader,
        )
        for request in requests
    ]


__all__ = [
    "RequestProcessingResult",
    "process_data_request",
    "process_pending_data_requests",
]
