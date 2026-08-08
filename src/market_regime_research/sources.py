"""
External market-data adapters and the local research data contract.

The existing per-ticker cache is optimized for the factor/backtest pipeline.
Turning-point research needs longer histories, explicit price semantics, source
timestamps, and stricter coverage checks, so it uses a separate cache under
``data/raw/market_regime``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Callable

import numpy as np
import pandas as pd
import requests

from src.data.fmp import get_historical_ohlcv_complete
from src.market_regime_research import SCHEMA_VERSION
from src.market_regime_research.models import DataContractError
from src.market_regime_research.settings import (
    MarketRegimeResearchSettings,
    PriceInstrumentSettings,
)
from src.utils.date_utils import parse_date_str
from src.utils.identifiers import canonical_ticker
from src.utils.logger import get_logger
from src.utils.market_calendar import latest_completed_xnys_session

log = get_logger(__name__)

CBOE_VOLATILITY_URLS = {
    "VIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "VIX9D": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv",
    "VIX3M": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
}
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
HY_OAS_SERIES_ID = "BAMLH0A0HYM2"

PRICE_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "volume",
    "available_at",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def symbol_storage_name(symbol: str) -> str:
    """Map a canonical market symbol to a portable, non-hidden filename."""
    value = canonical_ticker(symbol)
    if value.startswith("^"):
        value = f"INDEX_{value[1:]}"
    return value.replace(".", "_")


def price_path(
    settings: MarketRegimeResearchSettings,
    symbol: str,
) -> Path:
    return settings.prices_root / f"{symbol_storage_name(symbol)}.parquet"


def _availability_at(
    dates: pd.DatetimeIndex,
    *,
    hour: int,
    minute: int = 0,
) -> pd.DatetimeIndex:
    """Conservatively timestamp daily data after the US cash close."""
    local = (
        pd.DatetimeIndex(dates).tz_localize(None).normalize()
        + pd.Timedelta(hours=hour, minutes=minute)
    )
    return local.tz_localize(
        "America/New_York",
        ambiguous="raise",
        nonexistent="raise",
    ).tz_convert("UTC")


def _utc_timestamp(value: datetime | pd.Timestamp | None = None) -> pd.Timestamp:
    timestamp = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _xnys_sessions(
    start: str | pd.Timestamp,
    end: str | pd.Timestamp,
) -> pd.DatetimeIndex:
    """Return normalized XNYS sessions with bounds that cover old research data."""
    start_date = pd.Timestamp(start).tz_localize(None).normalize()
    end_date = pd.Timestamp(end).tz_localize(None).normalize()
    if start_date > end_date:
        return pd.DatetimeIndex([])
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required for market-data coverage validation"
        ) from exc
    calendar = xcals.get_calendar(
        "XNYS",
        start=(start_date - pd.Timedelta(days=14)).date().isoformat(),
        end=(end_date + pd.Timedelta(days=14)).date().isoformat(),
    )
    sessions = pd.DatetimeIndex(
        calendar.sessions_in_range(start_date, end_date)
    )
    if sessions.tz is not None:
        sessions = sessions.tz_localize(None)
    return sessions.normalize()


def _resolve_research_end(
    requested_end: str | pd.Timestamp,
    *,
    now: datetime | pd.Timestamp | None = None,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """
    Resolve the last session whose market and Cboe daily values are available.

    The cash close alone is insufficient: Cboe values are timestamped at 17:00
    America/New_York.  Before that cutoff, the prior XNYS session is the latest
    valid research date.
    """
    now_utc = _utc_timestamp(now)
    latest_completed = (
        latest_completed_xnys_session()
        if now is None
        else latest_completed_xnys_session(now=now_utc)
    )
    requested = pd.Timestamp(requested_end).tz_localize(None).normalize()
    upper_bound = min(requested, latest_completed)
    candidates = _xnys_sessions(
        upper_bound - pd.Timedelta(days=14),
        upper_bound,
    )
    if candidates.empty:
        raise DataContractError(
            f"No XNYS session found on or before {upper_bound.date()}"
        )
    resolved = pd.Timestamp(candidates[-1]).normalize()
    cutoff = _availability_at(
        pd.DatetimeIndex([resolved]),
        hour=17,
    )[0]
    if cutoff > now_utc:
        earlier = candidates[candidates < resolved]
        if earlier.empty:
            raise DataContractError("No prior XNYS session available before cutoff")
        resolved = pd.Timestamp(earlier[-1]).normalize()
    return resolved, latest_completed


def _normalized_ohlcv(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise DataContractError(f"{label} is empty")
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise DataContractError(f"{label} is missing OHLCV fields: {sorted(missing)}")
    out = frame[list(sorted(required))].copy()
    out.index = pd.to_datetime(out.index, errors="coerce")
    if out.index.isna().any():
        raise DataContractError(f"{label} contains invalid observation dates")
    if out.index.tz is not None:
        out.index = out.index.tz_convert(None)
    out.index = out.index.normalize()
    if out.index.has_duplicates:
        raise DataContractError(f"{label} contains duplicate observation dates")
    out = out.sort_index()
    for column in required:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if out[list(required)].isna().any().any():
        raise DataContractError(f"{label} contains missing/non-numeric OHLCV values")
    return out


def _validate_price_contract(
    frame: pd.DataFrame,
    *,
    symbol: str,
    available_not_after: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    required = set(PRICE_COLUMNS)
    missing = required - set(frame.columns)
    if frame.empty or missing:
        raise DataContractError(
            f"{symbol} price contract is empty or missing fields: {sorted(missing)}"
        )
    out = frame[list(PRICE_COLUMNS)].copy()
    out.index = pd.to_datetime(out.index, errors="coerce")
    if out.index.isna().any() or out.index.has_duplicates:
        raise DataContractError(f"{symbol} has invalid or duplicate dates")
    if out.index.tz is not None:
        out.index = out.index.tz_convert(None)
    out.index = out.index.normalize()
    out = out.sort_index()
    if not out.index.is_monotonic_increasing:
        raise DataContractError(f"{symbol} dates are not monotonic")

    price_columns = [
        "open",
        "high",
        "low",
        "close",
        "adj_open",
        "adj_high",
        "adj_low",
        "adj_close",
    ]
    for column in price_columns + ["volume"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if out[price_columns + ["volume"]].isna().any().any():
        raise DataContractError(f"{symbol} contains null market values")
    if (out[price_columns] <= 0).any().any() or (out["volume"] < 0).any():
        raise DataContractError(f"{symbol} contains non-positive prices/negative volume")

    raw_high_error = out["high"] + 1e-12 < out[["open", "close"]].max(axis=1)
    raw_low_error = out["low"] - 1e-12 > out[["open", "close"]].min(axis=1)
    adj_high_error = out["adj_high"] + 1e-12 < out[
        ["adj_open", "adj_close"]
    ].max(axis=1)
    adj_low_error = out["adj_low"] - 1e-12 > out[
        ["adj_open", "adj_close"]
    ].min(axis=1)
    if (raw_high_error | raw_low_error | adj_high_error | adj_low_error).any():
        raise DataContractError(f"{symbol} violates OHLC high/low invariants")

    available = pd.to_datetime(out["available_at"], errors="coerce", utc=True)
    if available.isna().any():
        raise DataContractError(f"{symbol} contains invalid available_at timestamps")
    expected_available = _availability_at(
        pd.DatetimeIndex(out.index),
        hour=16,
        minute=30,
    )
    if (pd.DatetimeIndex(available) < expected_available).any():
        raise DataContractError(
            f"{symbol} available_at predates the 16:30 EOD availability rule"
        )
    if available_not_after is not None and (
        pd.DatetimeIndex(available) > _utc_timestamp(available_not_after)
    ).any():
        raise DataContractError(
            f"{symbol} contains observations not yet available at runtime"
        )
    out["available_at"] = available
    out.index.name = "observation_date"
    return out


def combine_price_semantics(
    market_prices: pd.DataFrame,
    total_return_prices: pd.DataFrame | None,
    *,
    symbol: str,
) -> pd.DataFrame:
    """
    Combine tradable/chart OHLC with dividend-adjusted OHLC.

    FMP ``full`` prices are split-adjusted market prices.  FMP
    ``dividend-adjusted`` prices are used for total-return features.  Indexes do
    not have a dividend-adjusted series, so their adjusted columns equal market
    columns by construction.
    """
    market = _normalized_ohlcv(market_prices, label=f"{symbol} market prices")
    adjusted = (
        market
        if total_return_prices is None
        else _normalized_ohlcv(
            total_return_prices,
            label=f"{symbol} dividend-adjusted prices",
        )
    )
    if not market.index.equals(adjusted.index):
        missing_market = adjusted.index.difference(market.index)
        missing_adjusted = market.index.difference(adjusted.index)
        raise DataContractError(
            f"{symbol} market/adjusted calendars differ "
            f"(market_missing={len(missing_market)}, "
            f"adjusted_missing={len(missing_adjusted)})"
        )

    output = market[["open", "high", "low", "close", "volume"]].copy()
    output["adj_open"] = adjusted["open"]
    output["adj_high"] = adjusted["high"]
    output["adj_low"] = adjusted["low"]
    output["adj_close"] = adjusted["close"]
    output["available_at"] = _availability_at(
        pd.DatetimeIndex(output.index),
        hour=16,
        minute=30,
    )
    return _validate_price_contract(output, symbol=symbol)


def parse_cboe_history(payload: bytes | str, *, symbol: str) -> pd.DataFrame:
    """Parse one official Cboe daily volatility-index CSV."""
    raw = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
    frame = pd.read_csv(io.StringIO(raw))
    frame.columns = [str(column).strip().upper() for column in frame.columns]
    required = {"DATE", "OPEN", "HIGH", "LOW", "CLOSE"}
    if not required.issubset(frame.columns):
        raise DataContractError(
            f"Cboe {symbol} history missing fields: {sorted(required - set(frame.columns))}"
        )
    frame["DATE"] = pd.to_datetime(frame["DATE"], errors="coerce")
    if frame["DATE"].isna().any():
        raise DataContractError(f"Cboe {symbol} contains invalid dates")
    for column in ("OPEN", "HIGH", "LOW", "CLOSE"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame[["OPEN", "HIGH", "LOW", "CLOSE"]].isna().any().any():
        raise DataContractError(f"Cboe {symbol} contains non-numeric OHLC values")
    if (frame[["OPEN", "HIGH", "LOW", "CLOSE"]] <= 0).any().any():
        raise DataContractError(f"Cboe {symbol} contains non-positive OHLC values")
    if (frame["HIGH"] < frame["LOW"]).any():
        raise DataContractError(f"Cboe {symbol} has high below low")
    output = frame.set_index("DATE").sort_index()[["OPEN", "HIGH", "LOW", "CLOSE"]]
    if output.index.has_duplicates:
        raise DataContractError(f"Cboe {symbol} contains duplicate dates")
    output.columns = [
        f"{symbol}_open",
        f"{symbol}_high",
        f"{symbol}_low",
        symbol,
    ]
    output[f"{symbol}_available_at"] = _availability_at(
        pd.DatetimeIndex(output.index),
        hour=17,
    )
    output.index.name = "observation_date"
    return output


def _validate_volatility_contract(
    frame: pd.DataFrame,
    *,
    available_not_after: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Validate the outer-joined Cboe bundle without requiring common inception."""
    if frame is None or frame.empty:
        raise DataContractError("Cboe volatility cache is empty")
    output = frame.copy()
    output.index = pd.to_datetime(output.index, errors="coerce")
    if output.index.isna().any():
        raise DataContractError("Cboe volatility cache contains invalid dates")
    if output.index.tz is not None:
        output.index = output.index.tz_convert(None)
    output.index = output.index.normalize()
    if output.index.has_duplicates:
        raise DataContractError("Cboe volatility cache contains duplicate dates")
    output = output.sort_index()

    for symbol in CBOE_VOLATILITY_URLS:
        columns = [
            f"{symbol}_open",
            f"{symbol}_high",
            f"{symbol}_low",
            symbol,
            f"{symbol}_available_at",
        ]
        missing = set(columns) - set(output.columns)
        if missing:
            raise DataContractError(
                f"Cboe volatility cache missing fields: {sorted(missing)}"
            )
        price_columns = columns[:4]
        for column in price_columns:
            output[column] = pd.to_numeric(output[column], errors="coerce")
        any_price = output[price_columns].notna().any(axis=1)
        complete_price = output[price_columns].notna().all(axis=1)
        if (any_price != complete_price).any():
            raise DataContractError(f"Cboe {symbol} contains partial OHLC rows")
        observed = complete_price
        if not observed.any():
            raise DataContractError(f"Cboe volatility cache has no {symbol} values")
        if (output.loc[observed, price_columns] <= 0).any().any():
            raise DataContractError(f"Cboe {symbol} contains non-positive OHLC values")
        if (
            output.loc[observed, f"{symbol}_high"]
            < output.loc[observed, f"{symbol}_low"]
        ).any():
            raise DataContractError(f"Cboe {symbol} has high below low")

        available = pd.to_datetime(
            output[f"{symbol}_available_at"],
            errors="coerce",
            utc=True,
        )
        if available.loc[observed].isna().any():
            raise DataContractError(f"Cboe {symbol} contains invalid available_at")
        expected = _availability_at(
            pd.DatetimeIndex(output.index[observed]),
            hour=17,
        )
        observed_available = pd.DatetimeIndex(available.loc[observed])
        if (observed_available < expected).any():
            raise DataContractError(
                f"Cboe {symbol} available_at predates the 17:00 rule"
            )
        if available_not_after is not None and (
            observed_available > _utc_timestamp(available_not_after)
        ).any():
            raise DataContractError(
                f"Cboe {symbol} contains observations not yet available at runtime"
            )
        output[f"{symbol}_available_at"] = available

    output.index.name = "observation_date"
    return output


def _cboe_containment_anomalies(frame: pd.DataFrame, symbol: str) -> int:
    columns = [f"{symbol}_open", f"{symbol}_high", f"{symbol}_low", symbol]
    complete = frame[columns].notna().all(axis=1)
    sample = frame.loc[complete, columns]
    anomalies = (
        sample[f"{symbol}_high"] < sample[[f"{symbol}_open", symbol]].max(axis=1)
    ) | (
        sample[f"{symbol}_low"] > sample[[f"{symbol}_open", symbol]].min(axis=1)
    )
    return int(anomalies.sum())


def parse_fred_series(
    payload: bytes | str,
    *,
    series_id: str,
    output_name: str,
) -> pd.DataFrame:
    """Parse a FRED graph CSV and apply a conservative one-business-day lag."""
    raw = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
    frame = pd.read_csv(io.StringIO(raw))
    date_column = "observation_date" if "observation_date" in frame.columns else "DATE"
    if date_column not in frame.columns or series_id not in frame.columns:
        raise DataContractError(
            f"FRED {series_id} payload must contain {date_column} and {series_id}"
        )
    frame[date_column] = pd.to_datetime(frame[date_column], errors="coerce")
    frame[output_name] = pd.to_numeric(frame[series_id], errors="coerce")
    frame = frame.dropna(subset=[date_column]).set_index(date_column).sort_index()
    if frame.index.has_duplicates:
        raise DataContractError(f"FRED {series_id} contains duplicate dates")
    output = frame[[output_name]].copy()
    # FRED uses "." for holidays and unavailable observations.  Keep the
    # economic observation clock, not placeholder calendar rows.
    output = output.dropna(subset=[output_name])
    next_business_date = (
        pd.DatetimeIndex(output.index) + pd.offsets.BusinessDay(1)
    )
    output[f"{output_name}_available_at"] = _availability_at(
        next_business_date,
        hour=18,
    )
    output.index.name = "observation_date"
    return output


def _validate_credit_contract(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise DataContractError("FRED credit cache is empty")
    output = frame.copy()
    output.index = pd.to_datetime(output.index, errors="coerce")
    if output.index.isna().any():
        raise DataContractError("FRED credit cache contains invalid dates")
    if output.index.tz is not None:
        output.index = output.index.tz_convert(None)
    output.index = output.index.normalize()
    if output.index.has_duplicates:
        raise DataContractError("FRED credit cache contains duplicate dates")
    output = output.sort_index()
    required = {"hy_oas", "hy_oas_available_at"}
    missing = required - set(output.columns)
    if missing:
        raise DataContractError(
            f"FRED credit cache missing fields: {sorted(missing)}"
        )
    output["hy_oas"] = pd.to_numeric(output["hy_oas"], errors="coerce")
    if output["hy_oas"].isna().any() or (output["hy_oas"] < 0).any():
        raise DataContractError("FRED HY OAS contains missing or negative values")
    available = pd.to_datetime(
        output["hy_oas_available_at"],
        errors="coerce",
        utc=True,
    )
    if available.isna().any():
        raise DataContractError("FRED credit cache contains invalid available_at")
    expected = _availability_at(
        pd.DatetimeIndex(output.index) + pd.offsets.BusinessDay(1),
        hour=18,
    )
    if (pd.DatetimeIndex(available) < expected).any():
        raise DataContractError(
            "FRED HY OAS available_at predates the conservative release rule"
        )
    output["hy_oas_available_at"] = available
    output.index.name = "observation_date"
    return output


def _download_bytes(
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout: float = 60.0,
    retries: int = 3,
) -> bytes:
    last_error: Exception | None = None
    headers = {"User-Agent": "QuantResearch/1.0 source-audit"}
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.content
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (2 ** attempt))
    raise RuntimeError(f"Unable to download official source {url}: {last_error}")


def fetch_cboe_volatility_history(
    *,
    downloader: Callable[..., bytes] = _download_bytes,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Download and outer-join VIX, VIX9D, and VIX3M histories."""
    frames: list[pd.DataFrame] = []
    sources: list[dict[str, Any]] = []
    fetched_at = utc_now_iso()
    for symbol, url in CBOE_VOLATILITY_URLS.items():
        payload = downloader(url)
        frame = parse_cboe_history(payload, symbol=symbol)
        frames.append(frame)
        sources.append(
            {
                "instrument": symbol,
                "provider": "Cboe",
                "url": url,
                "payload_sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
                "rows": len(frame),
                "first_observation": frame.index.min().date().isoformat(),
                "last_observation": frame.index.max().date().isoformat(),
                "fetched_at": fetched_at,
                "availability_rule": "17:00 America/New_York on observation date",
                "ohlc_containment_anomalies": _cboe_containment_anomalies(
                    frame,
                    symbol,
                ),
            }
        )
    combined = _validate_volatility_contract(
        pd.concat(frames, axis=1).sort_index()
    )
    return combined, {"sources": sources}


def _cached_cboe_metadata(frame: pd.DataFrame) -> dict[str, Any]:
    """Recreate source-level lineage when a validated bundle is reused."""
    sources: list[dict[str, Any]] = []
    for symbol, url in CBOE_VOLATILITY_URLS.items():
        available_column = f"{symbol}_available_at"
        if symbol not in frame.columns or available_column not in frame.columns:
            raise DataContractError(
                f"Cached Cboe volatility bundle is missing "
                f"{symbol}/{available_column}"
            )
        available = pd.to_numeric(frame[symbol], errors="coerce").dropna()
        if available.empty:
            raise DataContractError(
                f"Cached Cboe volatility bundle has no {symbol} values"
            )
        sources.append(
            {
                "instrument": symbol,
                "provider": "Cboe",
                "url": url,
                "payload_sha256": None,
                "rows": len(available),
                "first_observation": available.index.min().date().isoformat(),
                "last_observation": available.index.max().date().isoformat(),
                "fetched_at": None,
                "availability_rule": "17:00 America/New_York on observation date",
                "ohlc_containment_anomalies": _cboe_containment_anomalies(
                    frame,
                    symbol,
                ),
                "cache_reused": True,
            }
        )
    return {"sources": sources}


def fetch_hy_oas_history(
    *,
    downloader: Callable[..., bytes] = _download_bytes,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Download the official FRED ICE BofA US HY option-adjusted spread."""
    payload = downloader(FRED_CSV_URL, params={"id": HY_OAS_SERIES_ID})
    frame = parse_fred_series(
        payload,
        series_id=HY_OAS_SERIES_ID,
        output_name="hy_oas",
    )
    frame = _validate_credit_contract(frame)
    metadata = {
        "instrument": "HY_OAS",
        "provider": "FRED",
        "series_id": HY_OAS_SERIES_ID,
        "url": f"{FRED_CSV_URL}?id={HY_OAS_SERIES_ID}",
        "payload_sha256": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "rows": len(frame),
        "first_observation": frame.index.min().date().isoformat(),
        "last_observation": frame.index.max().date().isoformat(),
        "fetched_at": utc_now_iso(),
        "availability_rule": (
            "conservative next business day 18:00 America/New_York; "
            "not a revision-vintage feed"
        ),
        "quality_status": "PASS",
        "validated_at": utc_now_iso(),
    }
    return frame, metadata


def download_price_history(
    instrument: PriceInstrumentSettings,
    *,
    end: str,
    fetcher: Callable[..., pd.DataFrame] = get_historical_ohlcv_complete,
) -> pd.DataFrame:
    """Download one complete index/ETF history with explicit price semantics."""
    market = fetcher(
        instrument.symbol,
        instrument.start,
        end,
        dividend_adjusted=False,
    )
    adjusted = None
    if instrument.kind == "etf":
        adjusted = fetcher(
            instrument.symbol,
            instrument.start,
            end,
            dividend_adjusted=True,
        )
    return combine_price_semantics(market, adjusted, symbol=instrument.symbol)


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp.parquet",
        dir=str(path.parent),
    )
    os.close(descriptor)
    try:
        frame.to_parquet(temporary_name, compression="snappy")
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _atomic_write_json(value: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _coverage_is_sufficient(
    frame: pd.DataFrame,
    *,
    expected_start: str,
    expected_end: str,
) -> bool:
    if frame.empty:
        return False
    observed = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
    expected = _xnys_sessions(expected_start, expected_end)
    if expected.empty:
        return False
    return observed.equals(expected)


def _price_coverage_error(
    frame: pd.DataFrame,
    *,
    expected_start: str,
    expected_end: str,
) -> str:
    observed = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
    expected = _xnys_sessions(expected_start, expected_end)
    missing = expected.difference(observed)
    extra = observed.difference(expected)
    return (
        f"missing_sessions={len(missing)} sample="
        f"{[value.date().isoformat() for value in missing[:5]]}; "
        f"non_session_rows={len(extra)} sample="
        f"{[value.date().isoformat() for value in extra[:5]]}"
    )


def _require_volatility_freshness(
    frame: pd.DataFrame,
    *,
    expected_end: pd.Timestamp,
) -> None:
    for symbol in CBOE_VOLATILITY_URLS:
        observed = pd.to_numeric(frame[symbol], errors="coerce").dropna()
        if observed.empty or pd.Timestamp(observed.index.max()).normalize() != expected_end:
            last = (
                pd.Timestamp(observed.index.max()).date().isoformat()
                if not observed.empty
                else None
            )
            raise DataContractError(
                f"Cboe {symbol} is stale: expected {expected_end.date()}, got {last}"
            )


def prepare_market_sources(
    settings: MarketRegimeResearchSettings,
    *,
    force: bool = False,
    include_credit: bool = True,
) -> dict[str, Any]:
    """Download, validate, and atomically publish the Stage-A source cache."""
    runtime_now = _utc_timestamp()
    requested_end = pd.Timestamp(parse_date_str(settings.end)).normalize()
    end_date, latest_completed = _resolve_research_end(
        requested_end,
        now=runtime_now,
    )
    end = end_date.strftime("%Y-%m-%d")
    settings.prices_root.mkdir(parents=True, exist_ok=True)
    source_entries: list[dict[str, Any]] = []
    validated_at = utc_now_iso()

    for instrument in settings.instruments:
        path = price_path(settings, instrument.symbol)
        frame: pd.DataFrame | None = None
        downloaded = False
        if path.exists() and not force:
            candidate = _validate_price_contract(
                pd.read_parquet(path),
                symbol=instrument.symbol,
            )
            # Some vendors expose an in-progress "daily" bar before the cash
            # session closes.  A prior cache can therefore contain a date that
            # is still in the future from an EOD research perspective.
            expected_sessions = _xnys_sessions(instrument.start, end_date)
            if expected_sessions.empty:
                raise DataContractError(
                    f"No XNYS sessions in configured range for {instrument.symbol}"
                )
            had_incomplete_rows = bool(
                (candidate.index < expected_sessions.min()).any()
                or (candidate.index > end_date).any()
            )
            candidate = candidate.loc[
                (candidate.index >= expected_sessions.min())
                & (candidate.index <= end_date)
            ]
            candidate = _validate_price_contract(
                candidate,
                symbol=instrument.symbol,
                available_not_after=runtime_now,
            )
            if _coverage_is_sufficient(
                candidate,
                expected_start=instrument.start,
                expected_end=end,
            ):
                frame = candidate
                if had_incomplete_rows:
                    _atomic_write_parquet(frame, path)
        if frame is None:
            log.info(
                "Preparing long market history %s (%s..%s)",
                instrument.symbol,
                instrument.start,
                end,
            )
            frame = download_price_history(instrument, end=end)
            frame = _validate_price_contract(
                frame,
                symbol=instrument.symbol,
                available_not_after=runtime_now,
            )
            downloaded = True
            if not _coverage_is_sufficient(
                frame,
                expected_start=instrument.start,
                expected_end=end,
            ):
                raise DataContractError(
                    f"{instrument.symbol} does not cover configured range "
                    f"{instrument.start}..{end}; "
                    + _price_coverage_error(
                        frame,
                        expected_start=instrument.start,
                        expected_end=end,
                    )
                )
            _atomic_write_parquet(frame, path)
        source_entries.append(
            {
                "instrument": instrument.symbol,
                "provider": "FMP",
                "kind": instrument.kind,
                "endpoint": (
                    "historical-price-eod/full"
                    + (
                        " + historical-price-eod/dividend-adjusted"
                        if instrument.kind == "etf"
                        else ""
                    )
                ),
                "path": str(path.relative_to(settings.raw_root)),
                "file_sha256": sha256_file(path),
                "rows": len(frame),
                "first_observation": frame.index.min().date().isoformat(),
                "last_observation": frame.index.max().date().isoformat(),
                "price_semantics": (
                    "FMP full split-adjusted OHLC plus FMP dividend-adjusted OHLC"
                    if instrument.kind == "etf"
                    else "index OHLC; adjusted columns equal index OHLC"
                ),
                "availability_rule": "16:30 America/New_York on observation date",
                "quality_status": "PASS",
                "validated_at": validated_at,
                "fetched_at": validated_at if downloaded else None,
                "cache_reused": not downloaded,
            }
        )

    volatility_downloaded = force or not settings.volatility_path.exists()
    if force or not settings.volatility_path.exists():
        volatility, volatility_metadata = fetch_cboe_volatility_history()
    else:
        volatility = _validate_volatility_contract(
            pd.read_parquet(settings.volatility_path)
        )
        volatility_metadata = _cached_cboe_metadata(volatility)
    had_incomplete_volatility = bool((volatility.index > end_date).any())
    volatility = volatility.loc[volatility.index <= end_date].sort_index()
    volatility = _validate_volatility_contract(
        volatility,
        available_not_after=runtime_now,
    )
    _require_volatility_freshness(volatility, expected_end=end_date)
    if volatility_downloaded or had_incomplete_volatility:
        _atomic_write_parquet(volatility, settings.volatility_path)
    for metadata in volatility_metadata["sources"]:
        symbol = str(metadata["instrument"])
        observed = pd.to_numeric(volatility[symbol], errors="coerce").dropna()
        metadata.update(
            {
                "rows": len(observed),
                "first_observation": observed.index.min().date().isoformat(),
                "last_observation": observed.index.max().date().isoformat(),
                "quality_status": "PASS",
                "validated_at": validated_at,
                "ohlc_containment_anomalies": _cboe_containment_anomalies(
                    volatility,
                    symbol,
                ),
            }
        )
    source_entries.extend(volatility_metadata["sources"])
    source_entries.append(
        {
            "instrument": "CBOE_VOLATILITY_BUNDLE",
            "provider": "Cboe",
            "path": str(settings.volatility_path.relative_to(settings.raw_root)),
            "file_sha256": sha256_file(settings.volatility_path),
            "rows": len(volatility),
            "first_observation": volatility.index.min().date().isoformat(),
            "last_observation": volatility.index.max().date().isoformat(),
            "quality_status": "PASS",
            "validated_at": validated_at,
        }
    )

    if include_credit:
        if force or not settings.credit_path.exists():
            credit, credit_metadata = fetch_hy_oas_history()
            credit_downloaded = True
        else:
            credit = _validate_credit_contract(
                pd.read_parquet(settings.credit_path)
            )
            credit_downloaded = False
            credit_metadata = {
                "instrument": "HY_OAS",
                "provider": "FRED",
                "path": str(settings.credit_path.relative_to(settings.raw_root)),
                "rows": len(credit),
                "quality_status": "PASS",
                "validated_at": validated_at,
            }
        credit_available = pd.to_datetime(
            credit["hy_oas_available_at"],
            errors="raise",
            utc=True,
        )
        had_unavailable_credit = bool((credit_available > runtime_now).any())
        credit = credit.loc[credit_available <= runtime_now].copy()
        if credit.empty:
            raise DataContractError(
                "FRED credit source has no observations available at runtime"
            )
        if credit_downloaded or had_unavailable_credit:
            _atomic_write_parquet(credit, settings.credit_path)
        credit_metadata.update(
            {
                "rows": len(credit),
                "first_observation": credit.index.min().date().isoformat(),
                "last_observation": credit.index.max().date().isoformat(),
            }
        )
        source_entries.append(credit_metadata)
        source_entries.append(
            {
                "instrument": "CREDIT_BUNDLE",
                "provider": "FRED",
                "path": str(settings.credit_path.relative_to(settings.raw_root)),
                "file_sha256": sha256_file(settings.credit_path),
                "rows": len(credit),
                "first_observation": credit.index.min().date().isoformat(),
                "last_observation": credit.index.max().date().isoformat(),
                "quality_status": "PASS",
                "validated_at": validated_at,
            }
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now_iso(),
        "configured_end": end,
        "requested_end": requested_end.date().isoformat(),
        "latest_completed_xnys_session": latest_completed.date().isoformat(),
        "credit_included": include_credit,
        "sources": source_entries,
    }
    _atomic_write_json(manifest, settings.source_manifest_path)
    return manifest


def load_prepared_prices(
    settings: MarketRegimeResearchSettings,
) -> dict[str, pd.DataFrame]:
    """Load every configured price series and re-run contract validation."""
    output: dict[str, pd.DataFrame] = {}
    missing: list[str] = []
    runtime_now = _utc_timestamp()
    requested_end = pd.Timestamp(parse_date_str(settings.end)).normalize()
    expected_end, _ = _resolve_research_end(
        requested_end,
        now=runtime_now,
    )
    for instrument in settings.instruments:
        path = price_path(settings, instrument.symbol)
        if not path.exists():
            missing.append(instrument.symbol)
            continue
        frame = _validate_price_contract(
            pd.read_parquet(path),
            symbol=instrument.symbol,
            available_not_after=runtime_now,
        )
        if not _coverage_is_sufficient(
            frame,
            expected_start=instrument.start,
            expected_end=expected_end.strftime("%Y-%m-%d"),
        ):
            raise DataContractError(
                f"{instrument.symbol} cache does not exactly cover configured "
                f"XNYS sessions through {expected_end.date()}; "
                + _price_coverage_error(
                    frame,
                    expected_start=instrument.start,
                    expected_end=expected_end.strftime("%Y-%m-%d"),
                )
                + ". Rerun prepare."
            )
        output[instrument.symbol] = frame
    if missing:
        raise FileNotFoundError(
            f"Prepared market price histories are missing: {missing}. "
            "Run the market-regime prepare command first."
        )
    return output


def load_prepared_volatility(
    settings: MarketRegimeResearchSettings,
) -> pd.DataFrame:
    """Load, validate, and freshness-check the prepared Cboe bundle."""
    if not settings.volatility_path.exists():
        raise FileNotFoundError(
            f"Missing Cboe volatility cache: {settings.volatility_path}"
        )
    runtime_now = _utc_timestamp()
    expected_end, _ = _resolve_research_end(
        pd.Timestamp(parse_date_str(settings.end)).normalize(),
        now=runtime_now,
    )
    frame = _validate_volatility_contract(
        pd.read_parquet(settings.volatility_path),
        available_not_after=runtime_now,
    )
    if (frame.index > expected_end).any():
        raise DataContractError(
            "Cboe volatility cache contains rows after the research cutoff; "
            "rerun prepare"
        )
    _require_volatility_freshness(frame, expected_end=expected_end)
    return frame


def load_prepared_credit(
    settings: MarketRegimeResearchSettings,
) -> pd.DataFrame:
    """Load credit history and remove observations not yet available at runtime."""
    if not settings.credit_path.exists():
        raise FileNotFoundError(f"Missing credit cache: {settings.credit_path}")
    frame = _validate_credit_contract(pd.read_parquet(settings.credit_path))
    available = pd.to_datetime(
        frame["hy_oas_available_at"],
        errors="raise",
        utc=True,
    )
    if (available > _utc_timestamp()).any():
        raise DataContractError(
            "FRED credit cache contains observations not yet available; "
            "rerun prepare"
        )
    return frame


__all__ = [
    "CBOE_VOLATILITY_URLS",
    "FRED_CSV_URL",
    "HY_OAS_SERIES_ID",
    "PRICE_COLUMNS",
    "combine_price_semantics",
    "download_price_history",
    "fetch_cboe_volatility_history",
    "fetch_hy_oas_history",
    "load_prepared_credit",
    "load_prepared_prices",
    "load_prepared_volatility",
    "parse_cboe_history",
    "parse_fred_series",
    "prepare_market_sources",
    "price_path",
    "sha256_file",
    "symbol_storage_name",
    "utc_now_iso",
]
