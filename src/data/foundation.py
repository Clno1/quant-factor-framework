"""
Versioned daily-market-data foundation backed by DuckDB and Parquet.

The module deliberately separates two roles:

* ``MarketDataWriter`` is the only network-aware component.  It downloads a
  completed XNYS session, merges a bounded revision window, validates the
  candidate, writes immutable Parquet files, then atomically advances the
  catalog's published pointer.
* ``MarketDataReader`` is network-free.  Research, backtests, and paper trading
  can only see a version after every configured quality check passed.

DuckDB is the small catalog and query engine; Parquet remains the durable
columnar store.  This keeps versions portable and makes a later migration to a
server database a catalog change rather than a rewrite of all historical bars.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

import numpy as np
import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.data.pit import (
    load_point_in_time_membership,
    point_in_time_required,
)
from src.data.universe import get_universe
from src.utils.date_utils import parse_date_str
from src.utils.file_lock import file_lock
from src.utils.identifiers import safe_path_component
from src.utils.logger import get_logger
from src.utils.market_calendar import (
    is_xnys_session,
    latest_publishable_xnys_session,
)

log = get_logger(__name__)

BAR_COLUMNS = [
    "date",
    "ticker",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
]
PRICE_COLUMNS = ["open", "high", "low", "close", "adj_close"]
Fetcher = Callable[[str, str, str], pd.DataFrame | None]
_VERIFIED_FILE_CACHE: set[tuple[str, int, int, str]] = set()
_VERIFIED_FILE_CACHE_LOCK = threading.Lock()


class DataFoundationError(RuntimeError):
    """Base exception for catalog, publication, and read-contract failures."""


class DataQualityError(DataFoundationError):
    """Raised when a candidate version fails one or more quality gates."""

    def __init__(self, universe: str, checks: list["QualityCheck"]):
        failed = [check for check in checks if not check.passed]
        detail = "; ".join(f"{c.name}: {c.message}" for c in failed)
        super().__init__(f"[{universe}] candidate data version rejected: {detail}")
        self.universe = universe
        self.checks = checks


@dataclass(frozen=True)
class QualityCheck:
    """One auditable publication gate."""

    name: str
    passed: bool
    observed: Any
    threshold: Any
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetVersion:
    """Catalog row describing one immutable curated market-data version."""

    version_id: str
    run_id: str
    universe: str
    provider: str
    status: str
    target_session: date
    created_at: datetime
    row_count: int
    ticker_count: int
    min_date: date | None
    max_date: date | None
    target_coverage: float
    bars_path: str
    universe_path: str
    membership_path: str | None
    membership_checksum_sha256: str | None
    manifest_path: str
    checksum_sha256: str
    universe_checksum_sha256: str | None = None
    manifest_checksum_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IngestionResult:
    """Outcome returned by the daily writer and rendered by the CLI."""

    run_id: str
    universe: str
    target_session: date
    status: str
    version: DatasetVersion | None
    fetched_tickers: int
    failed_tickers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["version"] = self.version.to_dict() if self.version else None
        return payload


def _foundation_setting(name: str, default: Any) -> Any:
    try:
        return getattr(CONFIG.data.foundation, name)
    except (AttributeError, KeyError):
        return default


def _configured_path(name: str, default: str) -> Path:
    raw = Path(str(_foundation_setting(name, default)))
    return raw if raw.is_absolute() else PROJECT_ROOT / raw


def default_catalog_path() -> Path:
    return _configured_path("catalog_path", "data/catalog/quant.duckdb")


def default_lake_dir() -> Path:
    return _configured_path("lake_dir", "data/lake")


def _duckdb_module() -> Any:
    try:
        import duckdb
    except ImportError as exc:
        raise DataFoundationError(
            "DuckDB is not installed. Run `python -m pip install -r "
            "requirements.txt` before using the data foundation."
        ) from exc
    return duckdb


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _portable_path(path: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path)


def _resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_file_sha256(path: Path, expected: str) -> None:
    """Verify an immutable file once per path/size/mtime/checksum tuple."""
    stat = path.stat()
    cache_key = (
        str(path.resolve()),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        str(expected),
    )
    with _VERIFIED_FILE_CACHE_LOCK:
        if cache_key in _VERIFIED_FILE_CACHE:
            return
    observed = _file_sha256(path)
    if observed != expected:
        raise DataFoundationError(
            f"Published file checksum mismatch: {path}; "
            f"expected={expected}, observed={observed}"
        )
    with _VERIFIED_FILE_CACHE_LOCK:
        _VERIFIED_FILE_CACHE.add(cache_key)


class MarketDataCatalog:
    """Small DuckDB catalog containing runs, checks, and publication pointers."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_catalog_path()

    @property
    def writer_lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".writer.lock")

    def _connect(self, *, read_only: bool) -> Any:
        duckdb = _duckdb_module()
        if read_only and not self.path.exists():
            raise DataFoundationError(
                f"DuckDB catalog does not exist: {self.path}. Run "
                "`python scripts/run_data_pipeline.py update` first."
            )
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        # DuckDB permits many read-only processes or one writer process.  A
        # publication transaction is intentionally short; readers retry across
        # that small lock window instead of silently falling back to old files.
        attempts = 5 if read_only else 10
        for attempt in range(attempts):
            try:
                return duckdb.connect(str(self.path), read_only=read_only)
            except Exception:
                if attempt + 1 >= attempts:
                    raise
                time.sleep(0.2 * min(attempt + 1, 5))
        raise AssertionError("unreachable")

    def initialize(self) -> None:
        connection = self._connect(read_only=False)
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_runs (
                    run_id VARCHAR PRIMARY KEY,
                    universe VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    target_session DATE NOT NULL,
                    status VARCHAR NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL,
                    finished_at TIMESTAMPTZ,
                    error_message VARCHAR
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS dataset_versions (
                    version_id VARCHAR PRIMARY KEY,
                    run_id VARCHAR NOT NULL,
                    universe VARCHAR NOT NULL,
                    provider VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    target_session DATE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL,
                    row_count BIGINT NOT NULL,
                    ticker_count BIGINT NOT NULL,
                    min_date DATE,
                    max_date DATE,
                    target_coverage DOUBLE NOT NULL,
                    bars_path VARCHAR NOT NULL,
                    universe_path VARCHAR NOT NULL,
                    membership_path VARCHAR,
                    membership_checksum_sha256 VARCHAR,
                    manifest_path VARCHAR NOT NULL,
                    checksum_sha256 VARCHAR NOT NULL,
                    universe_checksum_sha256 VARCHAR,
                    manifest_checksum_sha256 VARCHAR
                )
                """
            )
            # Keep catalogs created by an earlier development build readable.
            connection.execute(
                """
                ALTER TABLE dataset_versions
                ADD COLUMN IF NOT EXISTS membership_path VARCHAR
                """
            )
            connection.execute(
                """
                ALTER TABLE dataset_versions
                ADD COLUMN IF NOT EXISTS membership_checksum_sha256 VARCHAR
                """
            )
            connection.execute(
                """
                ALTER TABLE dataset_versions
                ADD COLUMN IF NOT EXISTS universe_checksum_sha256 VARCHAR
                """
            )
            connection.execute(
                """
                ALTER TABLE dataset_versions
                ADD COLUMN IF NOT EXISTS manifest_checksum_sha256 VARCHAR
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS quality_checks (
                    version_id VARCHAR NOT NULL,
                    check_name VARCHAR NOT NULL,
                    passed BOOLEAN NOT NULL,
                    observed_value VARCHAR,
                    threshold_value VARCHAR,
                    message VARCHAR NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS published_versions (
                    universe VARCHAR PRIMARY KEY,
                    version_id VARCHAR NOT NULL,
                    published_at TIMESTAMPTZ NOT NULL
                )
                """
            )
        finally:
            connection.close()

    def start_run(
        self,
        *,
        run_id: str,
        universe: str,
        provider: str,
        target_session: date,
    ) -> None:
        self.initialize()
        connection = self._connect(read_only=False)
        try:
            connection.execute(
                """
                INSERT INTO ingestion_runs
                (run_id, universe, provider, target_session, status, started_at)
                VALUES (?, ?, ?, ?, 'RUNNING', ?)
                """,
                [run_id, universe, provider, target_session, _utc_now()],
            )
        finally:
            connection.close()

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None:
        connection = self._connect(read_only=False)
        try:
            connection.execute(
                """
                UPDATE ingestion_runs
                SET status = ?, finished_at = ?, error_message = ?
                WHERE run_id = ?
                """,
                [status, _utc_now(), error_message, run_id],
            )
        finally:
            connection.close()

    def register_version(
        self,
        version: DatasetVersion,
        checks: list[QualityCheck],
        *,
        publish: bool,
    ) -> None:
        """Register checks and optionally advance the pointer in one transaction."""
        connection = self._connect(read_only=False)
        try:
            connection.execute("BEGIN TRANSACTION")
            connection.execute(
                """
                INSERT INTO dataset_versions
                (version_id, run_id, universe, provider, status,
                 target_session, created_at, row_count, ticker_count,
                 min_date, max_date, target_coverage, bars_path,
                 universe_path, membership_path,
                 membership_checksum_sha256, manifest_path, checksum_sha256,
                 universe_checksum_sha256, manifest_checksum_sha256)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    version.version_id,
                    version.run_id,
                    version.universe,
                    version.provider,
                    version.status,
                    version.target_session,
                    version.created_at,
                    version.row_count,
                    version.ticker_count,
                    version.min_date,
                    version.max_date,
                    version.target_coverage,
                    version.bars_path,
                    version.universe_path,
                    version.membership_path,
                    version.membership_checksum_sha256,
                    version.manifest_path,
                    version.checksum_sha256,
                    version.universe_checksum_sha256,
                    version.manifest_checksum_sha256,
                ],
            )
            for check in checks:
                connection.execute(
                    """
                    INSERT INTO quality_checks VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        version.version_id,
                        check.name,
                        check.passed,
                        _json_value(check.observed),
                        _json_value(check.threshold),
                        check.message,
                        _utc_now(),
                    ],
                )
            if publish:
                connection.execute(
                    "DELETE FROM published_versions WHERE universe = ?",
                    [version.universe],
                )
                connection.execute(
                    """
                    INSERT INTO published_versions VALUES (?, ?, ?)
                    """,
                    [version.universe, version.version_id, _utc_now()],
                )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _version_from_row(row: tuple[Any, ...]) -> DatasetVersion:
        return DatasetVersion(
            version_id=str(row[0]),
            run_id=str(row[1]),
            universe=str(row[2]),
            provider=str(row[3]),
            status=str(row[4]),
            target_session=pd.Timestamp(row[5]).date(),
            created_at=row[6],
            row_count=int(row[7]),
            ticker_count=int(row[8]),
            min_date=pd.Timestamp(row[9]).date() if row[9] is not None else None,
            max_date=pd.Timestamp(row[10]).date() if row[10] is not None else None,
            target_coverage=float(row[11]),
            bars_path=str(row[12]),
            universe_path=str(row[13]),
            membership_path=str(row[14]) if row[14] is not None else None,
            membership_checksum_sha256=(
                str(row[15]) if row[15] is not None else None
            ),
            manifest_path=str(row[16]),
            checksum_sha256=str(row[17]),
            universe_checksum_sha256=(
                str(row[18]) if row[18] is not None else None
            ),
            manifest_checksum_sha256=(
                str(row[19]) if row[19] is not None else None
            ),
        )

    @staticmethod
    def _version_projection(connection: Any, *, alias: str = "") -> str:
        """Read pre-v2 catalogs without mutating them from a reader process."""
        available = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info('dataset_versions')"
            ).fetchall()
        }
        prefix = f"{alias}." if alias else ""
        columns = [
            "version_id",
            "run_id",
            "universe",
            "provider",
            "status",
            "target_session",
            "created_at",
            "row_count",
            "ticker_count",
            "min_date",
            "max_date",
            "target_coverage",
            "bars_path",
            "universe_path",
            "membership_path",
            "membership_checksum_sha256",
            "manifest_path",
            "checksum_sha256",
            "universe_checksum_sha256",
            "manifest_checksum_sha256",
        ]
        return ", ".join(
            f"{prefix}{column}"
            if column in available
            else f"NULL AS {column}"
            for column in columns
        )

    def latest_version(self, universe: str) -> DatasetVersion | None:
        universe = safe_path_component(universe.upper(), label="universe")
        if not self.path.exists():
            return None
        connection = self._connect(read_only=True)
        try:
            projection = self._version_projection(connection, alias="d")
            row = connection.execute(
                f"""
                SELECT {projection}
                FROM published_versions AS p
                JOIN dataset_versions AS d ON d.version_id = p.version_id
                WHERE p.universe = ?
                """,
                [universe],
            ).fetchone()
        finally:
            connection.close()
        return self._version_from_row(row) if row is not None else None

    def get_version(
        self,
        version_id: str,
        *,
        universe: str | None = None,
    ) -> DatasetVersion | None:
        """Load one immutable published version instead of following the pointer."""
        if not self.path.exists():
            return None
        parameters: list[Any] = [str(version_id)]
        universe_clause = ""
        if universe is not None:
            universe_clause = " AND universe = ?"
            parameters.append(
                safe_path_component(universe.upper(), label="universe")
            )
        connection = self._connect(read_only=True)
        try:
            projection = self._version_projection(connection)
            row = connection.execute(
                f"""
                SELECT {projection}
                FROM dataset_versions
                WHERE version_id = ? AND status = 'PUBLISHED'{universe_clause}
                """,
                parameters,
            ).fetchone()
        finally:
            connection.close()
        return self._version_from_row(row) if row is not None else None

    def list_latest(self) -> list[DatasetVersion]:
        if not self.path.exists():
            return []
        connection = self._connect(read_only=True)
        try:
            projection = self._version_projection(connection, alias="d")
            rows = connection.execute(
                f"""
                SELECT {projection}
                FROM published_versions AS p
                JOIN dataset_versions AS d ON d.version_id = p.version_id
                ORDER BY d.universe
                """
            ).fetchall()
        finally:
            connection.close()
        return [self._version_from_row(row) for row in rows]

    def list_ingestion_runs(
        self,
        universe: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return recent writer attempts for one universe, including failures."""
        universe = safe_path_component(universe.upper(), label="universe")
        if not self.path.exists():
            return []
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT run_id, provider, target_session, status,
                       started_at, finished_at, error_message
                FROM ingestion_runs
                WHERE universe = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                [universe, max(1, min(int(limit), 100))],
            ).fetchall()
            columns = [item[0] for item in connection.description]
        finally:
            connection.close()
        return [dict(zip(columns, row, strict=True)) for row in rows]


def _normalize_universe_frame(
    frame: pd.DataFrame,
    *,
    target_session: date,
) -> pd.DataFrame:
    if "ticker" not in frame.columns:
        raise DataFoundationError("Universe frame must contain a ticker column")
    out = frame.copy()
    out["ticker"] = (
        out["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
        .str.replace(".", "-", regex=False)
    )
    out = out.loc[out["ticker"].ne("")].drop_duplicates("ticker", keep="last")
    for column in ("name", "sector", "sub_industry"):
        if column not in out.columns:
            out[column] = None
    out["is_current_member"] = True
    out["snapshot_date"] = pd.Timestamp(target_session)
    first = [
        "ticker",
        "name",
        "sector",
        "sub_industry",
        "is_current_member",
        "snapshot_date",
    ]
    return out[first + [c for c in out.columns if c not in first]].reset_index(
        drop=True
    )


def _historical_tickers(
    universe: str,
    *,
    start: pd.Timestamp,
    target: pd.Timestamp,
) -> tuple[set[str], pd.DataFrame | None, str | None]:
    membership, path = load_point_in_time_membership(universe)
    if membership is None or path is None:
        return set(), None, None
    snapshots = pd.DatetimeIndex(sorted(membership["date"].unique()))
    baseline = snapshots.searchsorted(start, side="right") - 1
    if baseline < 0:
        raise DataFoundationError(
            f"PIT membership for {universe} begins after ingestion start "
            f"{start.date()}"
        )
    relevant = set(
        snapshots[
            (snapshots >= snapshots[baseline])
            & (snapshots <= target)
        ]
    )
    version_membership = membership.loc[
        membership["date"].isin(relevant)
    ].copy()
    active = version_membership.loc[
        version_membership["active"], "ticker"
    ]
    return (
        set(active.astype(str)),
        version_membership.reset_index(drop=True),
        str(path),
    )


def _normalize_membership_frame(
    frame: pd.DataFrame,
    *,
    start: pd.Timestamp,
    target: pd.Timestamp,
) -> pd.DataFrame:
    required = {"date", "ticker", "active"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise DataFoundationError(
            f"Membership override is missing required columns: {missing}"
        )
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
    out["ticker"] = (
        out["ticker"]
        .astype(str)
        .str.strip()
        .str.upper()
        .str.replace(".", "-", regex=False)
    )
    out["active"] = out["active"].astype(bool)
    if out[["date", "ticker"]].isna().any().any():
        raise DataFoundationError("Membership override has invalid dates or tickers")
    out = (
        out.loc[out["date"].le(target)]
        .drop_duplicates(["date", "ticker"], keep="last")
        .sort_values(["date", "ticker"])
        .reset_index(drop=True)
    )
    snapshots = pd.DatetimeIndex(sorted(out["date"].unique()))
    if snapshots.empty or snapshots.searchsorted(start, side="right") == 0:
        raise DataFoundationError(
            f"Membership override has no baseline on or before {start.date()}"
        )
    return out


def _load_membership_events_for_version(
    membership_path: str | Path | None,
    *,
    start: pd.Timestamp,
    target: pd.Timestamp,
) -> pd.DataFrame | None:
    """Load and authenticate the PIT event ledger referenced by its metadata."""
    if not membership_path:
        return None
    source = Path(membership_path)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    metadata_path = source.with_suffix(".metadata.json")
    if not metadata_path.exists():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        source_meta = metadata.get("source") or {}
        raw_run_dir = Path(str(source_meta["raw_run_dir"]))
        events_path = raw_run_dir / "normalized_events.parquet"
        expected = str(source_meta["normalized_events_sha256"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataFoundationError(
            f"PIT metadata does not contain an event-ledger contract: {metadata_path}"
        ) from exc
    if not raw_run_dir.is_absolute():
        events_path = PROJECT_ROOT / events_path
    if not events_path.exists():
        raise DataFoundationError(
            f"PIT event ledger referenced by metadata is missing: {events_path}"
        )
    observed = _file_sha256(events_path)
    if expected.removeprefix("sha256:") != observed:
        raise DataFoundationError(
            f"PIT event ledger checksum mismatch: {events_path}"
        )
    events = pd.read_parquet(events_path)
    required = {"effective_date", "removed_ticker", "reason"}
    missing = sorted(required - set(events.columns))
    if missing:
        raise DataFoundationError(
            f"PIT event ledger is missing required columns: {missing}"
        )
    events = events.copy()
    events["effective_date"] = pd.to_datetime(
        events["effective_date"], errors="coerce"
    ).dt.normalize()
    if events["effective_date"].isna().any():
        raise DataFoundationError("PIT event ledger contains invalid dates")
    return (
        events.loc[
            events["effective_date"].between(start, target)
        ]
        .sort_values(["effective_date", "removed_ticker"], na_position="last")
        .reset_index(drop=True)
    )


def _observed_membership_frame(
    bars: pd.DataFrame,
    *,
    tickers: set[str],
    start: pd.Timestamp,
    target: pd.Timestamp,
) -> pd.DataFrame:
    """Build fixed-basket membership without treating pre-listing bars as missing."""
    normalized_tickers = sorted(str(ticker).upper() for ticker in tickers)
    work = bars.loc[
        bars["ticker"].astype(str).isin(normalized_tickers),
        ["date", "ticker"],
    ].copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.normalize()
    work = work.loc[work["date"].between(start, target)]
    first_dates = work.groupby("ticker")["date"].min().to_dict()
    snapshots = sorted(
        {start}
        | {
            max(start, pd.Timestamp(first_date).normalize())
            for first_date in first_dates.values()
            if pd.notna(first_date)
        }
    )
    rows = [
        {
            "date": snapshot,
            "ticker": ticker,
            "active": (
                ticker in first_dates
                and pd.Timestamp(first_dates[ticker]).normalize() <= snapshot
            ),
        }
        for snapshot in snapshots
        for ticker in normalized_tickers
    ]
    return pd.DataFrame(rows, columns=["date", "ticker", "active"])


def _normalize_download(
    ticker: str,
    frame: pd.DataFrame | None,
    *,
    start: pd.Timestamp,
    target: pd.Timestamp,
) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)
    work = frame.copy()
    if "date" not in work.columns:
        if isinstance(work.index, pd.DatetimeIndex):
            work.index.name = "date"
        work = work.reset_index()
    if "date" not in work.columns:
        raise DataFoundationError(f"{ticker}: downloaded frame has no date")
    missing = [column for column in BAR_COLUMNS[2:] if column not in work.columns]
    if missing:
        raise DataFoundationError(
            f"{ticker}: downloaded frame missing required columns {missing}"
        )
    work["date"] = pd.to_datetime(work["date"], errors="coerce", utc=True)
    work["date"] = work["date"].dt.tz_convert(None).dt.normalize()
    work["ticker"] = ticker
    for column in BAR_COLUMNS[2:]:
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.loc[
        work["date"].between(start, target, inclusive="both"), BAR_COLUMNS
    ]
    return (
        work.dropna(subset=["date"])
        .drop_duplicates(["date", "ticker"], keep="last")
        .sort_values(["date", "ticker"])
        .reset_index(drop=True)
    )


def _filter_non_xnys_bars(
    bars: pd.DataFrame,
    *,
    calendar: Any | None = None,
) -> tuple[pd.DataFrame, QualityCheck]:
    """Exclude vendor rows that are not official US exchange sessions."""
    if calendar is None:
        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise DataFoundationError(
                "exchange-calendars is required for daily-bar validation"
            ) from exc
        calendar = xcals.get_calendar("XNYS")

    dates = pd.to_datetime(bars["date"], errors="coerce").dt.normalize()
    valid_unique_dates = pd.DatetimeIndex(
        sorted(dates.loc[dates.notna()].unique())
    )
    invalid_dates = [
        pd.Timestamp(value)
        for value in valid_unique_dates
        if not calendar.is_session(pd.Timestamp(value))
    ]
    invalid_mask = dates.isin(invalid_dates)
    excluded = bars.loc[invalid_mask]
    filtered = bars.loc[~invalid_mask].reset_index(drop=True)
    remaining_dates = pd.to_datetime(
        filtered["date"], errors="coerce"
    ).dropna()
    remaining_invalid = sum(
        not calendar.is_session(pd.Timestamp(value))
        for value in pd.DatetimeIndex(sorted(remaining_dates.unique()))
    )
    check = QualityCheck(
        "xnys_session_dates",
        remaining_invalid == 0,
        {
            "excluded_rows": int(invalid_mask.sum()),
            "excluded_tickers": int(excluded["ticker"].nunique()),
            "excluded_date_count": len(invalid_dates),
            "excluded_date_sample": [
                value.date().isoformat() for value in invalid_dates[:20]
            ],
            "remaining_non_session_dates": int(remaining_invalid),
        },
        {"remaining_non_session_dates": 0},
        (
            f"excluded {int(invalid_mask.sum())} non-XNYS rows; "
            "all published dates are official sessions"
        ),
    )
    return filtered, check


def validate_daily_bars(
    bars: pd.DataFrame,
    *,
    current_tickers: Iterable[str],
    target_session: date,
    min_latest_coverage: float,
) -> list[QualityCheck]:
    """Run publication gates without mutating the candidate frame."""
    current = {str(ticker).upper() for ticker in current_tickers}
    target = pd.Timestamp(target_session)
    missing_columns = [column for column in BAR_COLUMNS if column not in bars.columns]
    checks: list[QualityCheck] = [
        QualityCheck(
            "required_schema",
            not missing_columns,
            missing_columns,
            [],
            "all required columns present"
            if not missing_columns
            else f"missing columns: {missing_columns}",
        )
    ]
    if missing_columns:
        return checks

    dates = pd.to_datetime(bars["date"], errors="coerce")
    numeric_columns = PRICE_COLUMNS + ["volume"]
    numeric = bars[numeric_columns].apply(pd.to_numeric, errors="coerce")
    invalid_dates = int(dates.isna().sum())
    checks.append(
        QualityCheck(
            "valid_dates",
            invalid_dates == 0,
            invalid_dates,
            0,
            "dates are valid"
            if invalid_dates == 0
            else f"{invalid_dates} invalid dates",
        )
    )

    duplicate_count = int(bars.duplicated(["date", "ticker"]).sum())
    checks.append(
        QualityCheck(
            "unique_date_ticker",
            duplicate_count == 0,
            duplicate_count,
            0,
            "date/ticker keys are unique"
            if duplicate_count == 0
            else f"{duplicate_count} duplicate keys",
        )
    )

    null_count = int(
        bars[["date", "ticker"]].isna().sum().sum()
        + numeric.isna().sum().sum()
    )
    checks.append(
        QualityCheck(
            "required_values_not_null",
            null_count == 0,
            null_count,
            0,
            "required values are complete"
            if null_count == 0
            else f"{null_count} required values are null",
        )
    )

    nonfinite_values = int(
        (~np.isfinite(numeric.to_numpy(dtype="float64"))).sum()
    )
    checks.append(
        QualityCheck(
            "finite_numeric_values",
            nonfinite_values == 0,
            nonfinite_values,
            0,
            "numeric values are finite"
            if nonfinite_values == 0
            else f"{nonfinite_values} non-finite numeric values",
        )
    )

    nonpositive_prices = int((numeric[PRICE_COLUMNS] <= 0).sum().sum())
    negative_volume = int((numeric["volume"] < 0).sum())
    checks.extend(
        [
            QualityCheck(
                "positive_prices",
                nonpositive_prices == 0,
                nonpositive_prices,
                0,
                "prices are positive"
                if nonpositive_prices == 0
                else f"{nonpositive_prices} non-positive prices",
            ),
            QualityCheck(
                "nonnegative_volume",
                negative_volume == 0,
                negative_volume,
                0,
                "volume is non-negative"
                if negative_volume == 0
                else f"{negative_volume} negative-volume rows",
            ),
        ]
    )

    tolerance = 1e-8
    invalid_ohlc = (
        (
            numeric["high"] + tolerance
            < numeric[["open", "close", "low"]].max(axis=1)
        )
        | (
            numeric["low"] - tolerance
            > numeric[["open", "close", "high"]].min(axis=1)
        )
    )
    invalid_ohlc_count = int(invalid_ohlc.sum())
    checks.append(
        QualityCheck(
            "ohlc_consistency",
            invalid_ohlc_count == 0,
            invalid_ohlc_count,
            0,
            "OHLC bounds are internally consistent"
            if invalid_ohlc_count == 0
            else f"{invalid_ohlc_count} rows violate OHLC bounds",
        )
    )

    future_count = int((dates > target).sum())
    checks.append(
        QualityCheck(
            "no_future_rows",
            future_count == 0,
            future_count,
            0,
            "candidate contains no post-target rows"
            if future_count == 0
            else f"{future_count} rows occur after target session",
        )
    )

    target_tickers = set(
        bars.loc[dates.eq(target), "ticker"]
        .astype(str)
        .str.upper()
    )
    covered = len(current & target_tickers)
    denominator = len(current)
    coverage = covered / denominator if denominator else 0.0
    checks.append(
        QualityCheck(
            "target_session_coverage",
            denominator > 0 and coverage >= float(min_latest_coverage),
            {
                "covered": covered,
                "current_tickers": denominator,
                "coverage": coverage,
                "missing_sample": sorted(current - target_tickers)[:20],
            },
            float(min_latest_coverage),
            f"target-session coverage {coverage:.2%}",
        )
    )
    return checks


def validate_pit_bar_coverage(
    bars: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    start: pd.Timestamp,
    target: pd.Timestamp,
    min_daily_coverage: float,
    calendar: Any | None = None,
) -> list[QualityCheck]:
    """
    Validate historical bars against each point-in-time membership snapshot.

    Checking only that a historical ticker appears as a matrix column is not
    sufficient: a removed constituent with one stale bar would pass that weak
    test and then disappear from most IC cross-sections.  This gate resolves
    the active set for every XNYS session and records the worst coverage.
    """
    if calendar is None:
        try:
            import exchange_calendars as xcals
        except ImportError as exc:
            raise DataFoundationError(
                "exchange-calendars is required for PIT history validation"
            ) from exc
        calendar = xcals.get_calendar("XNYS")

    snapshots = pd.DatetimeIndex(
        sorted(
            pd.to_datetime(membership["date"])
            .dt.normalize()
            .unique()
        )
    )
    sessions = pd.DatetimeIndex(
        calendar.sessions_in_range(
            start.date().isoformat(),
            target.date().isoformat(),
        )
    )
    if sessions.tz is not None:
        sessions = sessions.tz_localize(None)
    sessions = sessions.normalize()
    active_by_snapshot = {
        pd.Timestamp(snapshot): set(
            group.loc[group["active"], "ticker"].astype(str)
        )
        for snapshot, group in membership.groupby("date")
    }
    observed_by_date = {
        pd.Timestamp(session): set(group["ticker"].astype(str))
        for session, group in bars.groupby("date")
    }

    daily: list[tuple[pd.Timestamp, float, int, int]] = []
    relevant_active: set[str] = set()
    for session in sessions:
        position = snapshots.searchsorted(session, side="right") - 1
        if position < 0:
            daily.append((session, 0.0, 0, 0))
            continue
        active = active_by_snapshot.get(
            pd.Timestamp(snapshots[position]), set()
        )
        relevant_active.update(active)
        observed = observed_by_date.get(pd.Timestamp(session), set())
        covered = len(active & observed)
        coverage = covered / len(active) if active else 1.0
        daily.append((session, coverage, covered, len(active)))

    worst = min(daily, key=lambda item: item[1]) if daily else None
    worst_coverage = float(worst[1]) if worst is not None else 0.0
    daily_check = QualityCheck(
        "pit_daily_bar_coverage",
        bool(daily) and worst_coverage >= float(min_daily_coverage),
        {
            "sessions": len(daily),
            "worst_session": (
                worst[0].date().isoformat() if worst is not None else None
            ),
            "worst_coverage": worst_coverage,
            "covered": int(worst[2]) if worst is not None else 0,
            "active": int(worst[3]) if worst is not None else 0,
        },
        float(min_daily_coverage),
        f"worst PIT session coverage {worst_coverage:.2%}",
    )

    observed_tickers = set(bars["ticker"].astype(str))
    missing_tickers = sorted(relevant_active - observed_tickers)
    ticker_coverage = (
        len(relevant_active & observed_tickers) / len(relevant_active)
        if relevant_active
        else 0.0
    )
    ticker_check = QualityCheck(
        "pit_ticker_history_presence",
        bool(relevant_active) and not missing_tickers,
        {
            "coverage": ticker_coverage,
            "active_union": len(relevant_active),
            "missing_count": len(missing_tickers),
            "missing_sample": missing_tickers[:20],
        },
        1.0,
        f"PIT historical ticker presence {ticker_coverage:.2%}",
    )
    return [daily_check, ticker_check]


def _write_raw_ingestion(
    *,
    lake_dir: Path,
    provider: str,
    run_id: str,
    rows: pd.DataFrame,
    failures: list[str],
) -> Path:
    destination = (
        lake_dir
        / "raw"
        / provider
        / "eod"
        / f"ingestion_id={run_id}"
    )
    staging = destination.parent / f".staging_{run_id}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=False)
    rows.reindex(columns=BAR_COLUMNS).to_parquet(
        staging / "bars.parquet", index=False
    )
    (staging / "fetch_failures.json").write_text(
        json.dumps({"tickers": failures}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, destination)
    return destination / "bars.parquet"


def _write_curated_version(
    *,
    lake_dir: Path,
    universe: str,
    version_id: str,
    bars: pd.DataFrame,
    universe_frame: pd.DataFrame,
    membership_frame: pd.DataFrame | None,
    membership_events_frame: pd.DataFrame | None,
    manifest: dict[str, Any],
) -> tuple[Path, Path, Path | None, str | None, Path, str, str, str]:
    base = (
        lake_dir
        / "curated"
        / "equity_daily"
        / f"universe={universe}"
    )
    destination = base / f"version={version_id}"
    staging = base / f".staging_{version_id}"
    if destination.exists():
        raise DataFoundationError(f"Immutable version already exists: {destination}")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=False)

    bars_path = staging / "bars.parquet"
    universe_path = staging / "universe.parquet"
    membership_path = (
        staging / "membership.parquet"
        if membership_frame is not None
        else None
    )
    membership_events_path = (
        staging / "membership_events.parquet"
        if membership_events_frame is not None
        else None
    )
    bars.reindex(columns=BAR_COLUMNS).to_parquet(bars_path, index=False)
    universe_frame.to_parquet(universe_path, index=False)
    if membership_path is not None:
        membership_frame.to_parquet(membership_path, index=False)
    if membership_events_path is not None:
        membership_events_frame.to_parquet(
            membership_events_path,
            index=False,
        )
    checksum = _file_sha256(bars_path)
    universe_checksum = _file_sha256(universe_path)
    membership_checksum = (
        _file_sha256(membership_path)
        if membership_path is not None
        else None
    )
    membership_events_checksum = (
        _file_sha256(membership_events_path)
        if membership_events_path is not None
        else None
    )
    payload = {
        **manifest,
        "bars_sha256": checksum,
        "universe_sha256": universe_checksum,
        "membership_sha256": membership_checksum,
        "membership_events_path": (
            membership_events_path.name
            if membership_events_path is not None
            else None
        ),
        "membership_events_sha256": membership_events_checksum,
    }
    manifest_path = staging / "manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    manifest_checksum = _file_sha256(manifest_path)
    base.mkdir(parents=True, exist_ok=True)
    os.replace(staging, destination)
    return (
        destination / bars_path.name,
        destination / universe_path.name,
        (
            destination / membership_path.name
            if membership_path is not None
            else None
        ),
        membership_checksum,
        destination / manifest_path.name,
        checksum,
        universe_checksum,
        manifest_checksum,
    )


class MarketDataWriter:
    """Single-writer daily ingestion and publication service."""

    def __init__(
        self,
        *,
        catalog: MarketDataCatalog | None = None,
        lake_dir: str | Path | None = None,
        fetcher: Fetcher | None = None,
        profile_fetcher: Callable[[str], Mapping[str, Any] | None] | None = None,
    ):
        self.catalog = catalog or MarketDataCatalog()
        self.lake_dir = Path(lake_dir) if lake_dir is not None else default_lake_dir()
        self.fetcher = fetcher
        self.profile_fetcher = profile_fetcher

    def _fetcher(self) -> Fetcher:
        if self.fetcher is not None:
            return self.fetcher
        from src.data.fmp import get_historical_ohlcv

        return get_historical_ohlcv

    def update_universe(
        self,
        universe: str,
        *,
        target_session: str | date | pd.Timestamp | None = None,
        force: bool = False,
        workers: int | None = None,
        universe_frame: pd.DataFrame | None = None,
        initial_start: str | date | pd.Timestamp | None = None,
        membership_frame: pd.DataFrame | None = None,
        membership_source: str | None = None,
        derive_membership_from_bars: bool = False,
        min_latest_coverage: float | None = None,
    ) -> IngestionResult:
        universe = safe_path_component(universe.upper(), label="universe")
        delay = int(_foundation_setting("close_delay_minutes", 120))
        target = (
            latest_publishable_xnys_session(delay_minutes=delay)
            if target_session is None
            else pd.Timestamp(target_session).normalize()
        )
        target_date = target.date()
        if not is_xnys_session(target):
            raise DataFoundationError(
                f"{target_date} is not an XNYS trading session"
            )
        provider = str(getattr(CONFIG.data, "provider", "fmp")).strip().lower()
        run_id = uuid4().hex

        with file_lock(self.catalog.writer_lock_path):
            self.catalog.start_run(
                run_id=run_id,
                universe=universe,
                provider=provider,
                target_session=target_date,
            )
            try:
                latest = self.catalog.latest_version(universe)
                if latest is not None and latest.target_session > target_date:
                    raise DataFoundationError(
                        f"[{universe}] target {target_date} predates published "
                        f"version {latest.target_session}"
                    )
                if (
                    latest is not None
                    and latest.target_session == target_date
                    and not force
                ):
                    self.catalog.finish_run(run_id, status="NOOP")
                    return IngestionResult(
                        run_id=run_id,
                        universe=universe,
                        target_session=target_date,
                        status="NOOP",
                        version=latest,
                        fetched_tickers=0,
                        failed_tickers=(),
                    )

                current = _normalize_universe_frame(
                    universe_frame
                    if universe_frame is not None
                    else get_universe(universe, force_refresh=True),
                    target_session=target_date,
                )
                if current.empty:
                    raise DataFoundationError(f"[{universe}] current universe is empty")

                configured_start = pd.Timestamp(
                    initial_start
                    if initial_start is not None
                    else parse_date_str(CONFIG.date_range.start)
                ).normalize()
                start = configured_start
                if latest is not None and latest.min_date is not None:
                    start = min(
                        configured_start,
                        pd.Timestamp(latest.min_date).normalize(),
                    )
                if start > target:
                    raise DataFoundationError(
                        f"[{universe}] ingestion start {start.date()} is after "
                        f"target {target_date}"
                    )
                if membership_frame is not None:
                    pit_membership = _normalize_membership_frame(
                        membership_frame,
                        start=start,
                        target=target,
                    )
                    historical = set(
                        pit_membership.loc[
                            pit_membership["active"], "ticker"
                        ].astype(str)
                    )
                    pit_path = membership_source or "explicit_membership_override"
                    membership_events = None
                else:
                    historical, pit_membership, pit_path = _historical_tickers(
                        universe, start=start, target=target
                    )
                    membership_events = _load_membership_events_for_version(
                        pit_path,
                        start=start,
                        target=target,
                    )
                current_tickers = set(current["ticker"].astype(str))
                if point_in_time_required(universe):
                    if pit_membership is None or pit_path is None:
                        raise DataFoundationError(
                            f"[{universe}] point-in-time membership is required. "
                            "Run `python scripts/run_data_pipeline.py pit` "
                            "before daily market-data ingestion."
                        )
                    latest_snapshot = pd.Timestamp(
                        pit_membership["date"].max()
                    ).normalize()
                    if latest_snapshot != target:
                        raise DataFoundationError(
                            f"[{universe}] PIT membership ends at "
                            f"{latest_snapshot.date()}, but the target session "
                            f"is {target_date}. Refresh PIT before ingestion."
                        )
                    latest_members = set(
                        pit_membership.loc[
                            pit_membership["date"].eq(latest_snapshot)
                            & pit_membership["active"],
                            "ticker",
                        ].astype(str)
                    )
                    if latest_members != current_tickers:
                        missing = sorted(current_tickers - latest_members)[:20]
                        extra = sorted(latest_members - current_tickers)[:20]
                        raise DataFoundationError(
                            f"[{universe}] PIT current snapshot does not match "
                            f"the universe snapshot. missing={missing}, "
                            f"extra={extra}"
                        )
                all_tickers = sorted(current_tickers | historical)
                previous_metadata = None
                if latest is not None:
                    previous_universe_path = _resolve_path(latest.universe_path)
                    if previous_universe_path.exists():
                        previous_metadata = pd.read_parquet(previous_universe_path)
                profile_fetcher = self.profile_fetcher
                if profile_fetcher is None and self.fetcher is None:
                    from src.data.fmp import get_security_profile

                    profile_fetcher = get_security_profile
                from src.data.security_master import build_version_security_master

                metadata = build_version_security_master(
                    current,
                    tickers=all_tickers,
                    target_session=target_date,
                    membership=pit_membership,
                    previous=previous_metadata,
                    profile_fetcher=profile_fetcher,
                )

                previous = pd.DataFrame(columns=BAR_COLUMNS)
                if latest is not None:
                    previous_path = _resolve_path(latest.bars_path)
                    if not previous_path.exists():
                        raise DataFoundationError(
                            f"Published bars file is missing: {previous_path}"
                        )
                    previous = pd.read_parquet(previous_path).reindex(
                        columns=BAR_COLUMNS
                    )
                    previous["date"] = pd.to_datetime(previous["date"]).dt.normalize()

                overlap_days = int(
                    _foundation_setting("overlap_calendar_days", 21)
                )
                previous_max = (
                    previous.groupby("ticker")["date"].max().to_dict()
                    if not previous.empty
                    else {}
                )
                previous_min = (
                    previous.groupby("ticker")["date"].min().to_dict()
                    if not previous.empty
                    else {}
                )
                fetcher = self._fetcher()
                max_workers = max(
                    1,
                    int(
                        workers
                        if workers is not None
                        else _foundation_setting("max_workers", 6)
                    ),
                )

                def fetch_one(ticker: str) -> tuple[str, pd.DataFrame]:
                    ticker_last = previous_max.get(ticker)
                    ticker_first = previous_min.get(ticker)
                    fetch_start = start
                    history_already_covers_start = (
                        ticker_first is not None
                        and not pd.isna(ticker_first)
                        and pd.Timestamp(ticker_first).normalize() <= start
                    )
                    if (
                        history_already_covers_start
                        and ticker_last is not None
                        and not pd.isna(ticker_last)
                    ):
                        fetch_start = max(
                            start,
                            pd.Timestamp(ticker_last)
                            - pd.Timedelta(days=overlap_days),
                        )
                    frame = fetcher(
                        ticker,
                        fetch_start.date().isoformat(),
                        target_date.isoformat(),
                    )
                    return ticker, _normalize_download(
                        ticker,
                        frame,
                        start=fetch_start,
                        target=target,
                    )

                downloaded: list[pd.DataFrame] = []
                failures: list[str] = []
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(fetch_one, ticker): ticker
                        for ticker in all_tickers
                    }
                    for future in as_completed(futures):
                        ticker = futures[future]
                        try:
                            _, frame = future.result()
                        except Exception as exc:  # noqa: BLE001
                            failures.append(ticker)
                            log.warning("[%s] FMP fetch failed: %s", ticker, exc)
                            continue
                        if frame.empty:
                            failures.append(ticker)
                        else:
                            downloaded.append(frame)

                fetched = (
                    pd.concat(downloaded, ignore_index=True)
                    if downloaded
                    else pd.DataFrame(columns=BAR_COLUMNS)
                )
                raw_path = _write_raw_ingestion(
                    lake_dir=self.lake_dir,
                    provider=provider,
                    run_id=run_id,
                    rows=fetched,
                    failures=sorted(failures),
                )
                candidate = pd.concat([previous, fetched], ignore_index=True)
                candidate["date"] = pd.to_datetime(
                    candidate["date"], errors="coerce"
                ).dt.normalize()
                candidate = (
                    candidate.loc[candidate["date"].le(target), BAR_COLUMNS]
                    .drop_duplicates(["date", "ticker"], keep="last")
                    .sort_values(["date", "ticker"])
                    .reset_index(drop=True)
                )
                candidate, session_date_check = _filter_non_xnys_bars(candidate)

                if derive_membership_from_bars:
                    pit_membership = _observed_membership_frame(
                        candidate,
                        tickers=current_tickers,
                        start=start,
                        target=target,
                    )
                    pit_path = membership_source or "observed_daily_bars"

                coverage_threshold = float(
                    min_latest_coverage
                    if min_latest_coverage is not None
                    else _foundation_setting("min_latest_coverage", 0.98)
                )
                checks = validate_daily_bars(
                    candidate,
                    current_tickers=current_tickers,
                    target_session=target_date,
                    min_latest_coverage=coverage_threshold,
                )
                checks.append(session_date_check)
                if pit_membership is not None:
                    checks.extend(
                        validate_pit_bar_coverage(
                            candidate,
                            pit_membership,
                            start=start,
                            target=target,
                            min_daily_coverage=float(
                                _foundation_setting(
                                    "min_pit_daily_coverage",
                                    0.95,
                                )
                            ),
                        )
                    )
                fetched_target_tickers = set(
                    fetched.loc[
                        pd.to_datetime(fetched["date"]).eq(target),
                        "ticker",
                    ].astype(str)
                )
                fetched_current = len(
                    current_tickers & fetched_target_tickers
                )
                fetched_coverage = (
                    fetched_current / len(current_tickers)
                    if current_tickers
                    else 0.0
                )
                checks.append(
                    QualityCheck(
                        "provider_target_coverage",
                        (
                            bool(current_tickers)
                            and fetched_coverage >= coverage_threshold
                        ),
                        {
                            "covered": fetched_current,
                            "current_tickers": len(current_tickers),
                            "coverage": fetched_coverage,
                            "missing_sample": sorted(
                                current_tickers - fetched_target_tickers
                            )[:20],
                        },
                        coverage_threshold,
                        f"fresh provider target coverage "
                        f"{fetched_coverage:.2%}",
                    )
                )
                passed = all(check.passed for check in checks)
                coverage_check = next(
                    check
                    for check in checks
                    if check.name == "target_session_coverage"
                )
                observed = coverage_check.observed
                coverage = (
                    float(observed.get("coverage", 0.0))
                    if isinstance(observed, dict)
                    else 0.0
                )

                version_id = uuid4().hex
                created_at = _utc_now()
                manifest = {
                    "schema_version": 2,
                    "version_id": version_id,
                    "run_id": run_id,
                    "universe": universe,
                    "provider": provider,
                    "status": "PUBLISHED" if passed else "REJECTED",
                    "target_session": target_date,
                    "created_at": created_at,
                    "row_count": len(candidate),
                    "ticker_count": candidate["ticker"].nunique(),
                    "current_ticker_count": len(current_tickers),
                    "pit_membership_source": pit_path,
                    "pit_membership_source_sha256": (
                        _file_sha256(Path(pit_path))
                        if pit_path and Path(pit_path).is_file()
                        else None
                    ),
                    "pit_membership_rows": (
                        len(pit_membership)
                        if pit_membership is not None
                        else 0
                    ),
                    "pit_membership_event_rows": (
                        len(membership_events)
                        if membership_events is not None
                        else 0
                    ),
                    "raw_ingestion_path": _portable_path(raw_path),
                    "failed_tickers": sorted(failures),
                    "quality_checks": [check.to_dict() for check in checks],
                }
                from src.data.security_master import classification_coverage

                manifest["classification"] = classification_coverage(metadata)
                (
                    bars_path,
                    universe_path,
                    membership_path,
                    membership_checksum,
                    manifest_path,
                    checksum,
                    universe_checksum,
                    manifest_checksum,
                ) = _write_curated_version(
                    lake_dir=self.lake_dir,
                    universe=universe,
                    version_id=version_id,
                    bars=candidate,
                    universe_frame=metadata,
                    membership_frame=pit_membership,
                    membership_events_frame=membership_events,
                    manifest=manifest,
                )
                version = DatasetVersion(
                    version_id=version_id,
                    run_id=run_id,
                    universe=universe,
                    provider=provider,
                    status="PUBLISHED" if passed else "REJECTED",
                    target_session=target_date,
                    created_at=created_at,
                    row_count=len(candidate),
                    ticker_count=int(candidate["ticker"].nunique()),
                    min_date=(
                        pd.Timestamp(candidate["date"].min()).date()
                        if not candidate.empty
                        else None
                    ),
                    max_date=(
                        pd.Timestamp(candidate["date"].max()).date()
                        if not candidate.empty
                        else None
                    ),
                    target_coverage=coverage,
                    bars_path=_portable_path(bars_path),
                    universe_path=_portable_path(universe_path),
                    membership_path=(
                        _portable_path(membership_path)
                        if membership_path is not None
                        else None
                    ),
                    membership_checksum_sha256=membership_checksum,
                    manifest_path=_portable_path(manifest_path),
                    checksum_sha256=checksum,
                    universe_checksum_sha256=universe_checksum,
                    manifest_checksum_sha256=manifest_checksum,
                )
                self.catalog.register_version(version, checks, publish=passed)
                if not passed:
                    error = str(DataQualityError(universe, checks))
                    self.catalog.finish_run(
                        run_id, status="REJECTED", error_message=error
                    )
                    raise DataQualityError(universe, checks)
                self.catalog.finish_run(run_id, status="PUBLISHED")
                return IngestionResult(
                    run_id=run_id,
                    universe=universe,
                    target_session=target_date,
                    status="PUBLISHED",
                    version=version,
                    fetched_tickers=len(downloaded),
                    failed_tickers=tuple(sorted(failures)),
                )
            except DataQualityError:
                raise
            except Exception as exc:
                self.catalog.finish_run(
                    run_id, status="FAILED", error_message=str(exc)
                )
                raise


class MarketDataReader:
    """Read-only access to the latest quality-approved dataset version."""

    def __init__(self, *, catalog: MarketDataCatalog | None = None):
        self.catalog = catalog or MarketDataCatalog()

    def require_latest(self, universe: str) -> DatasetVersion:
        universe = safe_path_component(universe.upper(), label="universe")
        version = self.catalog.latest_version(universe)
        if version is None:
            raise DataFoundationError(
                f"[{universe}] no published DuckDB data version exists. Run "
                f"`python scripts/run_data_pipeline.py update --universe "
                f"{universe}` first. No legacy fallback is allowed."
            )
        self.verify_version(version)
        return version

    def require_version(self, universe: str, version_id: str) -> DatasetVersion:
        universe = safe_path_component(universe.upper(), label="universe")
        version = self.catalog.get_version(version_id, universe=universe)
        if version is None:
            raise DataFoundationError(
                f"[{universe}] published data version does not exist: {version_id}"
            )
        self.verify_version(version)
        return version

    def verify_version(self, version: DatasetVersion) -> dict[str, Any]:
        """Fail closed unless every immutable publication file is authenticated."""
        required_hashes = {
            "bars_sha256": version.checksum_sha256,
            "universe_sha256": version.universe_checksum_sha256,
            "manifest_sha256": version.manifest_checksum_sha256,
        }
        if version.membership_path is not None:
            required_hashes["membership_sha256"] = (
                version.membership_checksum_sha256
            )
        missing = sorted(key for key, value in required_hashes.items() if not value)
        if missing:
            raise DataFoundationError(
                f"[{version.universe}] data version {version.version_id} predates "
                f"the complete integrity contract; missing={missing}. Republish it."
            )

        files = {
            "bars": (_resolve_path(version.bars_path), version.checksum_sha256),
            "universe": (
                _resolve_path(version.universe_path),
                version.universe_checksum_sha256,
            ),
            "manifest": (
                _resolve_path(version.manifest_path),
                version.manifest_checksum_sha256,
            ),
        }
        if version.membership_path is not None:
            files["membership"] = (
                _resolve_path(version.membership_path),
                version.membership_checksum_sha256,
            )
        for label, (path, expected_sha256) in files.items():
            if not path.exists():
                raise DataFoundationError(
                    f"Published {label} file is missing: {path}"
                )
            _verify_file_sha256(path, str(expected_sha256))

        manifest_path = files["manifest"][0]
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DataFoundationError(
                f"Published manifest is unreadable: {manifest_path}"
            ) from exc
        expected = {
            "version_id": version.version_id,
            "run_id": version.run_id,
            "universe": version.universe,
            "target_session": version.target_session.isoformat(),
            "bars_sha256": version.checksum_sha256,
            "universe_sha256": version.universe_checksum_sha256,
            "membership_sha256": version.membership_checksum_sha256,
        }
        mismatches = [
            key
            for key, value in expected.items()
            if str(manifest.get(key)) != str(value)
        ]
        if mismatches:
            raise DataFoundationError(
                f"[{version.universe}] manifest/catalog mismatch for "
                f"{version.version_id}: {mismatches}"
            )
        events_name = manifest.get("membership_events_path")
        events_sha256 = manifest.get("membership_events_sha256")
        if bool(events_name) != bool(events_sha256):
            raise DataFoundationError(
                f"[{version.universe}] incomplete membership-event contract"
            )
        if events_name:
            events_path = (manifest_path.parent / str(events_name)).resolve()
            if events_path.parent != manifest_path.parent.resolve():
                raise DataFoundationError(
                    f"[{version.universe}] invalid membership-event path"
                )
            if not events_path.exists():
                raise DataFoundationError(
                    f"Published membership-event file is missing: {events_path}"
                )
            _verify_file_sha256(events_path, str(events_sha256))
        return manifest

    def _resolve_version(
        self,
        universe: str,
        version: DatasetVersion | str | None,
    ) -> DatasetVersion:
        if isinstance(version, DatasetVersion):
            if version.universe != universe.upper():
                raise DataFoundationError(
                    f"Data version {version.version_id} belongs to "
                    f"{version.universe}, not {universe}"
                )
            self.verify_version(version)
            return version
        if isinstance(version, str):
            return self.require_version(universe, version)
        return self.require_latest(universe)

    def _read_parquet(
        self,
        path_value: str,
        *,
        columns: Iterable[str] | None = None,
        expected_sha256: str | None = None,
    ) -> pd.DataFrame:
        path = _resolve_path(path_value)
        if not path.exists():
            raise DataFoundationError(f"Published Parquet file is missing: {path}")
        if expected_sha256:
            _verify_file_sha256(path, expected_sha256)
        connection = self.catalog._connect(read_only=True)
        try:
            projection = (
                ", ".join(f'"{column}"' for column in columns)
                if columns is not None
                else "*"
            )
            return connection.execute(
                f"""
                SELECT {projection}
                FROM read_parquet(?, hive_partitioning = false)
                """,
                [str(path)],
            ).fetchdf()
        finally:
            connection.close()

    def load_universe(
        self,
        universe: str,
        *,
        current_only: bool = True,
        version: DatasetVersion | str | None = None,
    ) -> pd.DataFrame:
        selected = self._resolve_version(universe, version)
        frame = self._read_parquet(
            selected.universe_path,
            expected_sha256=selected.universe_checksum_sha256,
        )
        if current_only and "is_current_member" in frame.columns:
            frame = frame.loc[frame["is_current_member"].astype(bool)]
        return frame.reset_index(drop=True)

    def load_membership(
        self,
        universe: str,
        *,
        version: DatasetVersion | str | None = None,
    ) -> pd.DataFrame | None:
        """Load the PIT snapshots frozen inside the published version."""
        selected = self._resolve_version(universe, version)
        if selected.membership_path is None:
            return None
        frame = self._read_parquet(
            selected.membership_path,
            expected_sha256=selected.membership_checksum_sha256,
        )
        required = {"date", "ticker", "active"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise DataFoundationError(
                f"[{universe}] published PIT membership is missing {missing}"
            )
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        frame["ticker"] = frame["ticker"].astype(str).str.upper()
        frame["active"] = frame["active"].astype(bool)
        return frame.sort_values(["date", "ticker"]).reset_index(drop=True)

    def load_membership_events(
        self,
        universe: str,
        *,
        version: DatasetVersion | str | None = None,
    ) -> pd.DataFrame | None:
        """Load the authenticated PIT add/remove event ledger, when published."""
        selected = self._resolve_version(universe, version)
        manifest = self.verify_version(selected)
        events_name = manifest.get("membership_events_path")
        events_sha256 = manifest.get("membership_events_sha256")
        if not events_name:
            return None
        events_path = _resolve_path(selected.manifest_path).parent / str(events_name)
        frame = self._read_parquet(
            str(events_path),
            expected_sha256=str(events_sha256),
        )
        required = {"effective_date", "removed_ticker", "reason"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise DataFoundationError(
                f"[{universe}] published membership events are missing {missing}"
            )
        frame["effective_date"] = pd.to_datetime(
            frame["effective_date"], errors="coerce"
        ).dt.normalize()
        if frame["effective_date"].isna().any():
            raise DataFoundationError(
                f"[{universe}] published membership events contain invalid dates"
            )
        return frame.sort_values(
            ["effective_date", "removed_ticker"],
            na_position="last",
        ).reset_index(drop=True)

    def load_bars(
        self,
        universe: str,
        *,
        tickers: Iterable[str] | None = None,
        start: str | date | pd.Timestamp | None = None,
        end: str | date | pd.Timestamp | None = None,
        version: DatasetVersion | str | None = None,
    ) -> pd.DataFrame:
        selected = self._resolve_version(universe, version)
        path = _resolve_path(selected.bars_path)
        if not path.exists():
            raise DataFoundationError(f"Published Parquet file is missing: {path}")
        _verify_file_sha256(path, selected.checksum_sha256)

        normalized_tickers = (
            sorted({str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()})
            if tickers is not None
            else None
        )
        if normalized_tickers == []:
            return pd.DataFrame(columns=BAR_COLUMNS)
        conditions: list[str] = []
        parameters: list[Any] = [str(path)]
        if normalized_tickers is not None:
            placeholders = ", ".join("?" for _ in normalized_tickers)
            conditions.append(f"upper(ticker) IN ({placeholders})")
            parameters.extend(normalized_tickers)
        if start is not None:
            conditions.append("date >= ?")
            parameters.append(pd.Timestamp(start).normalize().date())
        if end is not None:
            conditions.append("date <= ?")
            parameters.append(pd.Timestamp(end).normalize().date())
        where_clause = (
            "WHERE " + " AND ".join(conditions)
            if conditions
            else ""
        )
        projection = ", ".join(f'"{column}"' for column in BAR_COLUMNS)
        connection = self.catalog._connect(read_only=True)
        try:
            frame = connection.execute(
                f"""
                SELECT {projection}
                FROM read_parquet(?, hive_partitioning = false)
                {where_clause}
                """,
                parameters,
            ).fetchdf()
        finally:
            connection.close()
        frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
        frame["ticker"] = frame["ticker"].astype(str).str.upper()
        return frame.sort_values(["date", "ticker"]).reset_index(drop=True)

    def load_wide_tables(
        self,
        universe: str,
        *,
        tickers: Iterable[str] | None = None,
        require_open: bool = False,
        start: str | date | pd.Timestamp | None = None,
        end: str | date | pd.Timestamp | None = None,
        version: DatasetVersion | str | None = None,
    ) -> dict[str, pd.DataFrame]:
        requested = list(tickers) if tickers is not None else None
        selected = self._resolve_version(universe, version)
        bars = self.load_bars(
            universe,
            tickers=requested,
            start=start,
            end=end,
            version=selected,
        )
        if bars.empty:
            raise DataFoundationError(
                f"[{universe}] published version contains no requested bars"
            )
        if requested is not None:
            order = [str(ticker).upper() for ticker in requested]
            observed = set(bars["ticker"].astype(str).str.upper())
            order = [ticker for ticker in order if ticker in observed]
        else:
            order = sorted(set(bars["ticker"].astype(str)))

        out: dict[str, pd.DataFrame] = {}
        for field in ("close", "open", "high", "low", "adj_close", "volume"):
            matrix = bars.pivot(index="date", columns="ticker", values=field)
            matrix.index.name = "date"
            matrix.columns.name = "ticker"
            out[field] = matrix.sort_index().reindex(columns=order)
        out["returns"] = out["adj_close"].pct_change(fill_method=None)

        metadata = self.load_universe(
            universe,
            current_only=False,
            version=selected,
        )
        if "sector" not in metadata.columns:
            metadata["sector"] = None
        sector = (
            metadata.drop_duplicates("ticker", keep="last")
            .set_index("ticker")["sector"]
            .reindex(order)
            .rename("sector")
            .to_frame()
        )
        out["sector"] = sector
        if "market_cap" in metadata.columns:
            out["market_cap"] = (
                metadata.drop_duplicates("ticker", keep="last")
                .set_index("ticker")["market_cap"]
                .reindex(order)
                .rename("market_cap")
                .to_frame()
            )
        if require_open and (
            out["open"].empty or out["open"].isna().all(axis=None)
        ):
            raise DataFoundationError(
                f"[{universe}] published version has no usable open prices"
            )
        return out


__all__ = [
    "BAR_COLUMNS",
    "DataFoundationError",
    "DataQualityError",
    "DatasetVersion",
    "IngestionResult",
    "MarketDataCatalog",
    "MarketDataReader",
    "MarketDataWriter",
    "QualityCheck",
    "default_catalog_path",
    "default_lake_dir",
    "validate_daily_bars",
    "validate_pit_bar_coverage",
]
