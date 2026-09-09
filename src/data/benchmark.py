"""Version-bound benchmark loading for formal backtests.

A benchmark is part of a research result's data contract, not a display-only
fallback. Named research universes obtain their benchmark ticker from the
version-controlled research-universe registry (for example SP500 -> SPY and
NASDAQ100 -> QQQ). The loader first looks for that ticker in the primary
immutable dataset version and otherwise resolves a deterministic immutable
coverage version for the exact same target session.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Mapping

import pandas as pd

from src.data.foundation import DataFoundationError, DatasetVersion, MarketDataReader
from src.data.price_semantics import PriceSemantics
from src.data.universe_ids import US_EQUITY_COVERAGE
from src.research_universes import research_universe_registry


class BenchmarkDataError(DataFoundationError):
    """A formal benchmark cannot be resolved from authenticated market data."""


@dataclass(frozen=True)
class BenchmarkDataContract:
    schema_version: int
    ticker: str
    data_universe: str
    dataset_version_id: str
    dataset_run_id: str
    target_session: str
    bars_sha256: str
    manifest_sha256: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BenchmarkBundle:
    contract: BenchmarkDataContract
    total_return_open: pd.Series
    total_return_close: pd.Series
    holding_returns: pd.Series


def _benchmark_ticker(requested_universe: str) -> str:
    entry = research_universe_registry().get(str(requested_universe).upper())
    ticker = str(entry.benchmark or "").strip().upper()
    if not ticker:
        raise BenchmarkDataError(
            f"Research universe {requested_universe} has no registered benchmark"
        )
    return ticker


def _wide_from_bars(bars: pd.DataFrame, ticker: str) -> dict[str, pd.DataFrame]:
    required = {"date", "ticker", "open", "close", "adj_close", "volume"}
    missing = sorted(required - set(bars.columns))
    if missing:
        raise BenchmarkDataError(f"Benchmark bars are missing columns: {missing}")
    data = bars.copy()
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.normalize()
    data["ticker"] = data["ticker"].astype(str).str.upper()
    data = data.loc[data["ticker"].eq(ticker)].dropna(subset=["date"])
    if data.empty:
        raise BenchmarkDataError(f"Published data contain no bars for benchmark {ticker}")
    out: dict[str, pd.DataFrame] = {}
    for key in ("open", "close", "adj_close", "volume"):
        matrix = data.pivot(index="date", columns="ticker", values=key).sort_index()
        out[key] = matrix
    return out


def _contract(
    *,
    ticker: str,
    version: DatasetVersion,
    source: str,
) -> BenchmarkDataContract:
    return BenchmarkDataContract(
        schema_version=1,
        ticker=ticker,
        data_universe=version.universe,
        dataset_version_id=version.version_id,
        dataset_run_id=version.run_id,
        target_session=version.target_session.isoformat(),
        bars_sha256=version.checksum_sha256,
        manifest_sha256=version.manifest_checksum_sha256,
        source=source,
    )


def _validate_pinned_contract(
    payload: Mapping[str, Any],
    *,
    ticker: str,
    reader: MarketDataReader,
) -> DatasetVersion:
    if int(payload.get("schema_version") or 0) != 1:
        raise BenchmarkDataError("Unsupported benchmark data-contract schema")
    if str(payload.get("ticker") or "").strip().upper() != ticker:
        raise BenchmarkDataError(
            f"Pinned benchmark ticker mismatch: expected={ticker} "
            f"observed={payload.get('ticker')}"
        )
    data_universe = str(payload.get("data_universe") or "").strip().upper()
    version_id = str(payload.get("dataset_version_id") or "").strip()
    if not data_universe or not version_id:
        raise BenchmarkDataError("Pinned benchmark contract is missing version identity")
    version = reader.require_version(
        data_universe,
        version_id,
        require_price_semantics=True,
    )
    expected = {
        "dataset_run_id": version.run_id,
        "target_session": version.target_session.isoformat(),
        "bars_sha256": version.checksum_sha256,
        "manifest_sha256": version.manifest_checksum_sha256,
    }
    mismatches = [key for key, value in expected.items() if payload.get(key) != value]
    if mismatches:
        raise BenchmarkDataError(
            f"Pinned benchmark publication identity changed: {mismatches}"
        )
    return version


def _published_version_for_session(
    reader: MarketDataReader,
    universe: str,
    target_session: date,
) -> DatasetVersion | None:
    """Return a deterministic immutable publication for the exact target session.

    The mutable `published_versions` pointer is deliberately not used here. A
    stale ETF observation is not a valid benchmark for a newer strategy
    publication. If the exact session was corrected and republished, bind the
    newest immutable publication; the selected version id is then frozen in the
    task contract.
    """
    catalog = reader.catalog
    if not catalog.path.exists():
        return None
    connection = catalog._connect(read_only=True)  # noqa: SLF001 - integrity query
    try:
        projection = catalog._version_projection(connection)  # noqa: SLF001
        row = connection.execute(
            f"""
            SELECT {projection}
            FROM dataset_versions
            WHERE universe = ?
              AND status = 'PUBLISHED'
              AND target_session = ?
            ORDER BY created_at DESC, version_id DESC
            LIMIT 1
            """,
            [str(universe).upper(), target_session],
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    version = catalog._version_from_row(row)  # noqa: SLF001
    reader.verify_version(version, require_price_semantics=True)
    return version


def _ticker_exists_in_version(
    reader: MarketDataReader,
    version: DatasetVersion,
    ticker: str,
) -> bool:
    reader.verify_version(version, require_price_semantics=True)
    sample = reader.load_bars(
        version.universe,
        tickers=[ticker],
        start=version.target_session,
        end=version.target_session,
        version=version,
    )
    return not sample.empty


def _resolve_unpinned_source(
    requested_universe: str,
    *,
    primary_version: DatasetVersion | None,
    reader: MarketDataReader,
) -> tuple[str, DatasetVersion, str]:
    ticker = _benchmark_ticker(requested_universe)
    if primary_version is not None and _ticker_exists_in_version(
        reader, primary_version, ticker
    ):
        return ticker, primary_version, "PRIMARY_DATASET"

    if primary_version is not None:
        version = _published_version_for_session(
            reader,
            US_EQUITY_COVERAGE,
            primary_version.target_session,
        )
    else:
        try:
            version = reader.require_latest(
                US_EQUITY_COVERAGE,
                require_price_semantics=True,
            )
        except DataFoundationError:
            version = None
    if version is None:
        raise BenchmarkDataError(
            f"Registered benchmark {ticker} is not present in the primary "
            "publication and no deterministic immutable US_EQUITY_COVERAGE "
            "publication exists for the exact primary target session. Publish "
            "benchmark coverage before running a formal backtest."
        )
    if not _ticker_exists_in_version(reader, version, ticker):
        raise BenchmarkDataError(
            f"Resolved immutable benchmark publication {version.version_id} "
            f"does not contain {ticker} on target session {version.target_session}"
        )
    if primary_version is not None and version.target_session != primary_version.target_session:
        raise BenchmarkDataError(
            f"Benchmark publication target {version.target_session} does not match "
            f"primary target {primary_version.target_session}"
        )
    return ticker, version, "US_EQUITY_COVERAGE"


def resolve_registered_benchmark_contract(
    requested_universe: str,
    *,
    primary_version: DatasetVersion | None,
    pinned_contract: Mapping[str, Any] | None = None,
    reader: MarketDataReader | None = None,
) -> BenchmarkDataContract:
    """Resolve benchmark identity without loading its full history."""
    reader = reader or MarketDataReader()
    ticker = _benchmark_ticker(requested_universe)
    if pinned_contract:
        version = _validate_pinned_contract(
            pinned_contract,
            ticker=ticker,
            reader=reader,
        )
        if (
            primary_version is not None
            and version.target_session != primary_version.target_session
        ):
            raise BenchmarkDataError(
                "Pinned benchmark target session does not match the primary "
                f"publication: benchmark={version.target_session} "
                f"primary={primary_version.target_session}"
            )
        source = str(pinned_contract.get("source") or "PINNED")
        if not _ticker_exists_in_version(reader, version, ticker):
            raise BenchmarkDataError(
                f"Pinned benchmark publication {version.version_id} lacks {ticker}"
            )
        return _contract(ticker=ticker, version=version, source=source)

    ticker, version, source = _resolve_unpinned_source(
        requested_universe,
        primary_version=primary_version,
        reader=reader,
    )
    return _contract(ticker=ticker, version=version, source=source)


def load_registered_benchmark(
    requested_universe: str,
    *,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    primary_version: DatasetVersion | None = None,
    pinned_contract: Mapping[str, Any] | None = None,
    reader: MarketDataReader | None = None,
) -> BenchmarkBundle:
    """Load the registered benchmark and same-interval next-open total return.

    ``holding_returns`` is labelled on decision date t and represents the
    interval [t open, t+1 open), matching the formal next-open backtest.
    """
    reader = reader or MarketDataReader()
    contract = resolve_registered_benchmark_contract(
        requested_universe,
        primary_version=primary_version,
        pinned_contract=pinned_contract,
        reader=reader,
    )
    version = reader.require_version(
        contract.data_universe,
        contract.dataset_version_id,
        require_price_semantics=True,
    )
    bars = reader.load_bars(
        version.universe,
        tickers=[contract.ticker],
        start=start,
        end=end,
        version=version,
    )
    if bars.empty:
        raise BenchmarkDataError(
            f"Published benchmark {contract.ticker} has no rows in the requested period"
        )
    wide = _wide_from_bars(bars, contract.ticker)
    semantics = PriceSemantics.from_wide(wide)
    ticker = contract.ticker
    total_return_open = semantics.total_return_open[ticker].rename(ticker)
    total_return_close = semantics.total_return_close[ticker].rename(ticker)
    holding_returns = (
        total_return_open.pct_change(fill_method=None).shift(-1).rename("Benchmark")
    )
    return BenchmarkBundle(
        contract=contract,
        total_return_open=total_return_open,
        total_return_close=total_return_close,
        holding_returns=holding_returns,
    )


__all__ = [
    "BenchmarkBundle",
    "BenchmarkDataContract",
    "BenchmarkDataError",
    "load_registered_benchmark",
    "resolve_registered_benchmark_contract",
]
