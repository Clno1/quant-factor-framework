"""Strict, version-bound market-data access for all trading consumers.

Consumers never call FMP and never read ``data/raw/ohlcv``.  They either receive
one immutable published dataset version or a structured not-ready error that can
be attached to the centralized ingestion request queue.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping

import pandas as pd

from src.config import CONFIG
from src.data.foundation import (
    DataFoundationError,
    DatasetVersion,
    MarketDataReader,
)
from src.factors.publication import (
    ResearchPublicationError,
    dataset_version_provenance,
    validate_factor_research_publication,
)
from src.storage import DataRequest, app_database
from src.utils.identifiers import canonical_ticker, safe_path_component
from src.utils.market_calendar import (
    latest_publishable_xnys_session,
    xnys_session_on_or_before,
)


DEFAULT_WARMUP_CALENDAR_DAYS = 400


@dataclass(frozen=True)
class DataCoverage:
    data_universe: str
    requested_tickers: tuple[str, ...]
    observed_tickers: tuple[str, ...]
    missing_tickers: tuple[str, ...]
    unexpected_tickers: tuple[str, ...]
    requested_start: str | None
    requested_end: str | None
    required_history_start: str | None
    membership_start: str | None
    expected_session: str
    observed_session: str | None
    latest_coverage: float
    open_coverage: float
    min_date_by_ticker: dict[str, str]
    max_date_by_ticker: dict[str, str]
    passed: bool
    failures: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DataContract:
    schema_version: int
    requested_universe: str
    data_universe: str
    dataset_version_id: str
    dataset_run_id: str
    target_session: str
    bars_sha256: str
    membership_sha256: str | None
    factor_publication_id: str | None
    factor_generations: dict[str, str]
    runtime_factor_id: str | None
    coverage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishedDataBundle:
    version: DatasetVersion
    wide: dict[str, pd.DataFrame]
    membership: pd.DataFrame | None
    contract: DataContract
    publication: dict[str, Any] | None = None


class MarketDataNotReadyError(DataFoundationError):
    """A consumer cannot run until the centralized writer publishes data."""

    def __init__(
        self,
        message: str,
        *,
        data_universe: str,
        coverage: DataCoverage | None = None,
        request_id: str | None = None,
    ):
        super().__init__(message)
        self.data_universe = data_universe
        self.coverage = coverage
        self.request_id = request_id


def _setting(name: str, default: Any) -> Any:
    try:
        return getattr(CONFIG.data.foundation, name)
    except (AttributeError, KeyError):
        return default


def _normalize_tickers(tickers: Iterable[str] | None) -> list[str]:
    if tickers is None:
        return []
    return list(
        dict.fromkeys(
            canonical_ticker(ticker)
            for ticker in tickers
            if str(ticker).strip()
        )
    )


def _expected_session(end: str | pd.Timestamp | None) -> pd.Timestamp:
    latest = latest_publishable_xnys_session(
        delay_minutes=int(_setting("close_delay_minutes", 120))
    )
    if end is None:
        return latest
    requested = xnys_session_on_or_before(pd.Timestamp(end))
    return min(latest, requested)


def _date_map(frame: pd.DataFrame, operation: str) -> dict[str, str]:
    if frame.empty:
        return {}
    grouped = frame.groupby("ticker")["date"]
    series = grouped.min() if operation == "min" else grouped.max()
    return {
        str(ticker): pd.Timestamp(value).strftime("%Y-%m-%d")
        for ticker, value in series.items()
    }


def inspect_coverage(
    reader: MarketDataReader,
    *,
    data_universe: str,
    version: DatasetVersion,
    tickers: Iterable[str] | None,
    start: str | pd.Timestamp | None,
    end: str | pd.Timestamp | None,
    history_start: str | pd.Timestamp | None,
    require_open: bool,
    exact_universe: bool,
    min_latest_coverage: float,
) -> tuple[DataCoverage, pd.DataFrame]:
    requested = _normalize_tickers(tickers)
    expected_session = _expected_session(end)
    bars = reader.load_bars(
        data_universe,
        tickers=requested or None,
        end=expected_session,
        version=version,
    )
    observed = sorted(set(bars["ticker"].astype(str))) if not bars.empty else []
    requested_set = set(requested)
    observed_set = set(observed)
    missing = sorted(requested_set - observed_set)

    current = reader.load_universe(
        data_universe,
        current_only=True,
        version=version,
    )
    current_set = (
        set(current["ticker"].astype(str).str.upper())
        if "ticker" in current.columns
        else set()
    )
    unexpected = sorted(current_set - requested_set) if requested else []
    membership_missing = sorted(requested_set - current_set) if requested else []

    latest_rows = (
        bars.loc[pd.to_datetime(bars["date"]).eq(expected_session)]
        if not bars.empty
        else bars
    )
    latest_observed = set(latest_rows["ticker"].astype(str))
    denominator = len(requested_set) if requested_set else len(current_set)
    expected_set = requested_set if requested_set else current_set
    latest_coverage = (
        len(expected_set & latest_observed) / denominator if denominator else 0.0
    )
    if bars.empty:
        open_coverage = 0.0
    else:
        open_values = pd.to_numeric(bars["open"], errors="coerce")
        open_coverage = float(open_values.notna().mean())

    failures: list[str] = []
    if bars.empty:
        failures.append("no_bars")
    if missing:
        failures.append("missing_tickers")
    if membership_missing:
        failures.append("universe_membership_missing")
    if exact_universe and unexpected:
        failures.append("unexpected_current_members")
    if version.target_session < expected_session.date():
        failures.append("stale_target_session")
    if latest_coverage < float(min_latest_coverage):
        failures.append("latest_session_coverage")
    if require_open and open_coverage < float(min_latest_coverage):
        failures.append("open_coverage")

    membership_start: pd.Timestamp | None = None
    if exact_universe and history_start is not None:
        membership = reader.load_membership(
            data_universe,
            version=version,
        )
        if membership is None or membership.empty:
            failures.append("membership_history_missing")
        else:
            membership_start = pd.Timestamp(membership["date"].min()).normalize()
            if membership_start > pd.Timestamp(history_start).normalize():
                failures.append("insufficient_membership_history")

    observed_session = (
        pd.Timestamp(bars["date"].max()).strftime("%Y-%m-%d")
        if not bars.empty
        else None
    )
    coverage = DataCoverage(
        data_universe=data_universe,
        requested_tickers=tuple(requested),
        observed_tickers=tuple(observed),
        missing_tickers=tuple(sorted(set(missing) | set(membership_missing))),
        unexpected_tickers=tuple(unexpected),
        requested_start=(
            pd.Timestamp(start).strftime("%Y-%m-%d") if start is not None else None
        ),
        requested_end=(
            pd.Timestamp(end).strftime("%Y-%m-%d") if end is not None else None
        ),
        required_history_start=(
            pd.Timestamp(history_start).strftime("%Y-%m-%d")
            if history_start is not None
            else None
        ),
        membership_start=(
            membership_start.strftime("%Y-%m-%d")
            if membership_start is not None
            else None
        ),
        expected_session=expected_session.strftime("%Y-%m-%d"),
        observed_session=observed_session,
        latest_coverage=latest_coverage,
        open_coverage=open_coverage,
        min_date_by_ticker=_date_map(bars, "min"),
        max_date_by_ticker=_date_map(bars, "max"),
        passed=not failures,
        failures=tuple(failures),
    )
    return coverage, bars


def runtime_factor_id(
    *,
    version: DatasetVersion,
    factor_ids: Iterable[str],
) -> str:
    payload = {
        "dataset_version_id": version.version_id,
        "factor_ids": sorted(set(str(value) for value in factor_ids)),
        "preprocessing": dict(CONFIG.preprocessing),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"runtime:{digest}"


def load_published_bundle(
    *,
    requested_universe: str,
    data_universe: str | None = None,
    tickers: Iterable[str] | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    require_open: bool = False,
    exact_universe: bool = False,
    factor_ids: Iterable[str] | None = None,
    require_factor_publication: bool = False,
    dataset_version_id: str | None = None,
    factor_publication_id: str | None = None,
    reader: MarketDataReader | None = None,
) -> PublishedDataBundle:
    """Load one immutable version and enforce every requested identity."""
    reader = reader or MarketDataReader()
    selected_universe = safe_path_component(
        str(data_universe or requested_universe).upper(),
        label="data_universe",
    )
    try:
        version = (
            reader.require_version(selected_universe, dataset_version_id)
            if dataset_version_id
            else reader.require_latest(selected_universe)
        )
    except DataFoundationError as exc:
        raise MarketDataNotReadyError(
            str(exc),
            data_universe=selected_universe,
        ) from exc

    min_coverage = float(
        _setting(
            "custom_universe_min_coverage" if tickers else "min_latest_coverage",
            1.0 if tickers else 0.98,
        )
    )
    factors = list(dict.fromkeys(str(value) for value in (factor_ids or [])))
    history_start = None
    if start is not None and factors:
        history_start = (
            pd.Timestamp(start) - pd.Timedelta(days=DEFAULT_WARMUP_CALENDAR_DAYS)
        ).normalize()
    coverage, _ = inspect_coverage(
        reader,
        data_universe=selected_universe,
        version=version,
        tickers=tickers,
        start=start,
        end=end,
        history_start=history_start,
        require_open=require_open,
        exact_universe=exact_universe,
        min_latest_coverage=min_coverage,
    )
    if not coverage.passed:
        raise MarketDataNotReadyError(
            f"[{selected_universe}] published data does not satisfy the "
            f"consumer contract: {list(coverage.failures)}",
            data_universe=selected_universe,
            coverage=coverage,
        )

    publication: dict[str, Any] | None = None
    if require_factor_publication:
        try:
            publication = validate_factor_research_publication(
                selected_universe,
                version=version,
                factor_ids=factors,
                publication_id=factor_publication_id,
            )
        except ResearchPublicationError as exc:
            raise MarketDataNotReadyError(
                str(exc),
                data_universe=selected_universe,
            ) from exc

    load_start = None
    if start is not None:
        load_start = (
            pd.Timestamp(start) - pd.Timedelta(days=DEFAULT_WARMUP_CALENDAR_DAYS)
        ).normalize()
    wide = reader.load_wide_tables(
        selected_universe,
        tickers=tickers,
        require_open=require_open,
        start=load_start,
        end=coverage.expected_session,
        version=version,
    )
    membership = reader.load_membership(
        selected_universe,
        version=version,
    )
    factor_generations = {
        factor_id: str(payload.get("generation_id") or "")
        for factor_id, payload in (
            (publication or {}).get("factors") or {}
        ).items()
        if factor_id in factors
    }
    contract = DataContract(
        schema_version=1,
        requested_universe=requested_universe,
        data_universe=selected_universe,
        dataset_version_id=version.version_id,
        dataset_run_id=version.run_id,
        target_session=version.target_session.isoformat(),
        bars_sha256=version.checksum_sha256,
        membership_sha256=version.membership_checksum_sha256,
        factor_publication_id=(
            str(publication.get("publication_id"))
            if publication is not None
            else None
        ),
        factor_generations=factor_generations,
        runtime_factor_id=(
            None
            if require_factor_publication
            else runtime_factor_id(version=version, factor_ids=factors)
        ),
        coverage=coverage.to_dict(),
    )
    return PublishedDataBundle(
        version=version,
        wide=wide,
        membership=membership,
        contract=contract,
        publication=publication,
    )


def current_named_contract(
    universe: str,
    *,
    factor_ids: Iterable[str],
    reader: MarketDataReader | None = None,
) -> DataContract:
    """Resolve lightweight named-universe identities at task creation time."""
    reader = reader or MarketDataReader()
    factors = list(dict.fromkeys(str(value) for value in factor_ids))
    version = reader.require_latest(universe)
    publication = validate_factor_research_publication(
        universe,
        version=version,
        factor_ids=factors,
    )
    return DataContract(
        schema_version=1,
        requested_universe=universe,
        data_universe=universe,
        dataset_version_id=version.version_id,
        dataset_run_id=version.run_id,
        target_session=version.target_session.isoformat(),
        bars_sha256=version.checksum_sha256,
        membership_sha256=version.membership_checksum_sha256,
        factor_publication_id=str(publication["publication_id"]),
        factor_generations={
            factor_id: str(payload.get("generation_id") or "")
            for factor_id, payload in publication["factors"].items()
            if factor_id in set(factors)
        },
        runtime_factor_id=None,
        coverage={},
    )


def enqueue_market_data_request(
    *,
    data_universe: str,
    universe_frame: pd.DataFrame,
    start: str,
    end: str | None,
    consumer_kind: str,
    consumer_id: str,
    initial_start: str | None = None,
    force: bool = False,
) -> DataRequest:
    records = json.loads(
        universe_frame.reset_index(drop=True).to_json(
            orient="records",
            date_format="iso",
        )
    )
    payload = {
        "schema_version": 1,
        "data_universe": safe_path_component(
            data_universe.upper(),
            label="data_universe",
        ),
        "universe_records": records,
        "tickers": _normalize_tickers(universe_frame["ticker"].tolist()),
        "start": pd.Timestamp(start).strftime("%Y-%m-%d"),
        "end": (
            pd.Timestamp(end).strftime("%Y-%m-%d") if end is not None else None
        ),
        "initial_start": (
            pd.Timestamp(initial_start).strftime("%Y-%m-%d")
            if initial_start is not None
            else pd.Timestamp(start).strftime("%Y-%m-%d")
        ),
        "force": bool(force),
    }
    return app_database().enqueue_data_request(
        data_universe=data_universe,
        payload=payload,
        consumer_kind=consumer_kind,
        consumer_id=consumer_id,
    )


def watchlist_universe_frame(snapshot: Mapping[str, Any]) -> pd.DataFrame:
    rows = []
    for item in snapshot.get("items") or []:
        ticker = canonical_ticker(item.get("ticker"))
        rows.append(
            {
                "ticker": ticker,
                "name": str(item.get("name") or ""),
                "sector": None,
                "sub_industry": None,
                "watchlist_id": str(snapshot.get("id") or ""),
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise ValueError("Watchlist snapshot contains no tickers")
    return frame.drop_duplicates("ticker", keep="last").reset_index(drop=True)


__all__ = [
    "DataContract",
    "DataCoverage",
    "MarketDataNotReadyError",
    "PublishedDataBundle",
    "current_named_contract",
    "enqueue_market_data_request",
    "inspect_coverage",
    "load_published_bundle",
    "runtime_factor_id",
    "watchlist_universe_frame",
]
