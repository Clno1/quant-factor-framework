"""Strict, version-bound market-data access for all trading consumers.

Consumers never call FMP and never read ``data/raw/ohlcv``.  They either receive
one immutable published dataset version or a structured not-ready error that can
be attached to the centralized ingestion request queue.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
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
from src.data.benchmark import (
    BenchmarkDataError,
    load_registered_benchmark,
    resolve_registered_benchmark_contract,
)
from src.data.price_semantics import PriceSemantics, validate_price_semantics_contract
from src.factors.publication import (
    ResearchPublicationError,
    dataset_version_provenance,
    validate_factor_research_publication,
)
from src.storage import DataRequest, app_database
from src.utils.identifiers import canonical_ticker, safe_path_component
from src.utils.market_calendar import (
    latest_publishable_xnys_session,
    xnys_session_on_or_after,
    xnys_session_on_or_before,
)
from src.research_universes.registry import ResearchUniverseRegistryError


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
    universe_sha256: str | None = None
    manifest_sha256: str | None = None
    price_semantics: dict[str, Any] = field(default_factory=dict)
    benchmark: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PublishedDataBundle:
    version: DatasetVersion
    wide: dict[str, pd.DataFrame]
    universe: pd.DataFrame
    membership: pd.DataFrame | None
    membership_events: pd.DataFrame | None
    contract: DataContract
    publication: dict[str, Any] | None = None
    prices: PriceSemantics | None = None
    benchmark_returns: pd.Series = field(default_factory=pd.Series)


@dataclass
class PublishedDailyDataBundle:
    """Version-bound long-form daily data for non-factor consumers."""

    version: DatasetVersion
    bars: pd.DataFrame
    universe: pd.DataFrame
    membership: pd.DataFrame | None
    contract: DataContract


@dataclass
class PublishedUniverseBundle:
    """Frozen universe metadata and the version that owns it."""

    version: DatasetVersion
    universe: pd.DataFrame


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


def resolve_published_version(
    *,
    requested_universe: str,
    data_universe: str | None = None,
    dataset_version_id: str | None = None,
    reader: MarketDataReader | None = None,
) -> DatasetVersion:
    """Resolve one named version through the consumer access boundary."""
    reader = reader or MarketDataReader()
    selected_universe = safe_path_component(
        str(data_universe or requested_universe).upper(),
        label="data_universe",
    )
    try:
        return (
            reader.require_version(
                selected_universe,
                dataset_version_id,
                require_price_semantics=True,
            )
            if dataset_version_id
            else reader.require_latest(
                selected_universe,
                require_price_semantics=True,
            )
        )
    except DataFoundationError as exc:
        raise MarketDataNotReadyError(
            str(exc),
            data_universe=selected_universe,
        ) from exc


def load_published_universe(
    *,
    requested_universe: str,
    data_universe: str | None = None,
    dataset_version_id: str | None = None,
    reader: MarketDataReader | None = None,
) -> PublishedUniverseBundle:
    """Load frozen current-member metadata without reading the bars table."""
    reader = reader or MarketDataReader()
    version = resolve_published_version(
        requested_universe=requested_universe,
        data_universe=data_universe,
        dataset_version_id=dataset_version_id,
        reader=reader,
    )
    universe = reader.load_universe(
        version.universe,
        current_only=True,
        version=version,
    )
    return PublishedUniverseBundle(version=version, universe=universe)


def validate_daily_data_contract(
    contract: DataContract | Mapping[str, Any],
    *,
    reader: MarketDataReader | None = None,
) -> DatasetVersion:
    """Verify that a persisted daily contract still names the same publication."""
    payload = contract.to_dict() if isinstance(contract, DataContract) else dict(contract)
    data_universe = str(payload.get("data_universe") or "").strip().upper()
    version_id = str(payload.get("dataset_version_id") or "").strip()
    schema_version = int(payload.get("schema_version") or 0)
    if schema_version != 3 or not data_universe or not version_id:
        raise MarketDataNotReadyError(
            "Persisted daily data contract has an unsupported schema or missing identity",
            data_universe=data_universe or "UNKNOWN",
        )
    version = resolve_published_version(
        requested_universe=str(payload.get("requested_universe") or data_universe),
        data_universe=data_universe,
        dataset_version_id=version_id,
        reader=reader,
    )
    expected = {
        "dataset_run_id": version.run_id,
        "target_session": version.target_session.isoformat(),
        "bars_sha256": version.checksum_sha256,
        "membership_sha256": version.membership_checksum_sha256,
    }
    manifest = (reader or MarketDataReader()).verify_version(
        version,
        require_price_semantics=True,
    )
    expected.update(
        {
            "universe_sha256": version.universe_checksum_sha256,
            "manifest_sha256": version.manifest_checksum_sha256,
            "price_semantics": validate_price_semantics_contract(
                manifest.get("price_semantics")
            ),
        }
    )
    mismatches = [
        key
        for key, value in expected.items()
        if payload.get(key) != value
    ]
    if mismatches:
        raise MarketDataNotReadyError(
            "Persisted daily data contract no longer matches its publication: "
            f"{mismatches}",
            data_universe=data_universe,
        )
    return version


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
    bars_start: str | pd.Timestamp | None = None,
    allow_missing_tickers: bool = False,
) -> tuple[DataCoverage, pd.DataFrame]:
    requested = _normalize_tickers(tickers)
    expected_session = _expected_session(end)
    bars = reader.load_bars(
        data_universe,
        tickers=requested or None,
        start=bars_start,
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
    if missing and not allow_missing_tickers:
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

    if history_start is not None:
        required_session = xnys_session_on_or_after(history_start)
        version_start = (
            pd.Timestamp(version.min_date).normalize()
            if version.min_date is not None
            else None
        )
        if version_start is None or version_start > required_session:
            failures.append("insufficient_bar_history")

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


def _resolve_bundle_benchmark(
    *,
    requested_universe: str,
    data_universe: str,
    version: DatasetVersion,
    prices: PriceSemantics,
    start: str | pd.Timestamp | None,
    end: str | pd.Timestamp | None,
    reader: MarketDataReader,
) -> tuple[pd.Series, dict[str, Any]]:
    """Return a typed, version-bound benchmark for every backtest bundle."""
    try:
        benchmark = load_registered_benchmark(
            requested_universe,
            start=start,
            end=end,
            primary_version=version,
            reader=reader,
        )
    except ResearchUniverseRegistryError:
        basket = (
            prices.total_return_open.pct_change(fill_method=None).shift(-1).mean(axis=1)
        ).rename("Benchmark")
        contract = {
            "schema_version": 1,
            "ticker": None,
            "data_universe": data_universe,
            "dataset_version_id": version.version_id,
            "dataset_run_id": version.run_id,
            "target_session": version.target_session.isoformat(),
            "bars_sha256": version.checksum_sha256,
            "manifest_sha256": version.manifest_checksum_sha256,
            "source": "UNREGISTERED_EQUAL_WEIGHT_TOTAL_RETURN_BASKET",
        }
        return basket, contract
    except BenchmarkDataError as exc:
        raise MarketDataNotReadyError(
            "Formal named-universe data are missing their exact-session immutable "
            f"benchmark: {exc}",
            data_universe=data_universe,
        ) from exc
    return benchmark.holding_returns, benchmark.contract.to_dict()


def load_published_bundle(
    *,
    requested_universe: str,
    data_universe: str | None = None,
    tickers: Iterable[str] | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    require_open: bool = False,
    exact_universe: bool = False,
    required_history_start: str | pd.Timestamp | None = None,
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
            reader.require_version(
                selected_universe,
                dataset_version_id,
                require_price_semantics=True,
            )
            if dataset_version_id
            else reader.require_latest(
                selected_universe,
                require_price_semantics=True,
            )
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
    history_candidates: list[pd.Timestamp] = []
    if required_history_start is not None:
        history_candidates.append(pd.Timestamp(required_history_start).normalize())
    if start is not None and factors:
        history_candidates.append(
            (
                pd.Timestamp(start)
                - pd.Timedelta(days=DEFAULT_WARMUP_CALENDAR_DAYS)
            ).normalize()
        )
    history_start = min(history_candidates) if history_candidates else None
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

    load_start_candidates: list[pd.Timestamp] = []
    if start is not None:
        load_start_candidates.append(
            (
                pd.Timestamp(start)
                - pd.Timedelta(days=DEFAULT_WARMUP_CALENDAR_DAYS)
            ).normalize()
        )
    if history_start is not None:
        load_start_candidates.append(history_start)
    load_start = min(load_start_candidates) if load_start_candidates else None
    wide = reader.load_wide_tables(
        selected_universe,
        tickers=tickers,
        require_open=require_open,
        start=load_start,
        end=coverage.expected_session,
        version=version,
    )
    prices = PriceSemantics.from_wide(wide)
    benchmark_returns, benchmark_contract = _resolve_bundle_benchmark(
        requested_universe=requested_universe,
        data_universe=selected_universe,
        version=version,
        prices=prices,
        start=load_start,
        end=coverage.expected_session,
        reader=reader,
    )
    membership = reader.load_membership(
        selected_universe,
        version=version,
    )
    membership_events = reader.load_membership_events(
        selected_universe,
        version=version,
    )
    universe_metadata = reader.load_universe(
        selected_universe,
        current_only=False,
        version=version,
    )
    factor_generations = {
        factor_id: str(payload.get("generation_id") or "")
        for factor_id, payload in (
            (publication or {}).get("factors") or {}
        ).items()
        if factor_id in factors
    }
    manifest = reader.verify_version(version, require_price_semantics=True)
    contract = DataContract(
        schema_version=3,
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
        universe_sha256=version.universe_checksum_sha256,
        manifest_sha256=version.manifest_checksum_sha256,
        price_semantics=validate_price_semantics_contract(
            manifest.get("price_semantics")
        ),
        benchmark=benchmark_contract,
    )
    return PublishedDataBundle(
        version=version,
        wide=wide,
        universe=universe_metadata,
        membership=membership,
        membership_events=membership_events,
        contract=contract,
        publication=publication,
        prices=prices,
        benchmark_returns=benchmark_returns,
    )


def load_published_daily_data(
    *,
    requested_universe: str,
    data_universe: str | None = None,
    tickers: Iterable[str] | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    require_open: bool = False,
    exact_universe: bool = False,
    required_history_start: str | pd.Timestamp | None = None,
    dataset_version_id: str | None = None,
    min_latest_coverage: float | None = None,
    lookback_calendar_days: int | None = None,
    include_membership: bool = False,
    reader: MarketDataReader | None = None,
) -> PublishedDailyDataBundle:
    """Load one immutable daily version without constructing factor wide tables."""
    reader = reader or MarketDataReader()
    selected_universe = safe_path_component(
        str(data_universe or requested_universe).upper(),
        label="data_universe",
    )
    version = resolve_published_version(
        requested_universe=requested_universe,
        data_universe=selected_universe,
        dataset_version_id=dataset_version_id,
        reader=reader,
    )
    effective_start = start
    if effective_start is None and lookback_calendar_days is not None:
        days = int(lookback_calendar_days)
        if days < 1:
            raise ValueError("lookback_calendar_days must be positive")
        anchor = min(
            pd.Timestamp(version.target_session),
            _expected_session(end),
        )
        effective_start = (anchor - pd.Timedelta(days=days)).normalize()

    threshold = (
        float(min_latest_coverage)
        if min_latest_coverage is not None
        else float(
            _setting(
                "custom_universe_min_coverage" if tickers else "min_latest_coverage",
                1.0 if tickers else 0.98,
            )
        )
    )
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("min_latest_coverage must be between 0 and 1")
    coverage, bars = inspect_coverage(
        reader,
        data_universe=selected_universe,
        version=version,
        tickers=tickers,
        start=effective_start,
        end=end,
        history_start=required_history_start,
        require_open=require_open,
        exact_universe=exact_universe,
        min_latest_coverage=threshold,
        bars_start=effective_start,
        allow_missing_tickers=threshold < 1.0,
    )
    if not coverage.passed:
        raise MarketDataNotReadyError(
            f"[{selected_universe}] published daily data does not satisfy the "
            f"consumer contract: {list(coverage.failures)}",
            data_universe=selected_universe,
            coverage=coverage,
        )

    if effective_start is not None:
        bars = bars.loc[
            pd.to_datetime(bars["date"]).ge(pd.Timestamp(effective_start).normalize())
        ].copy()
    universe = reader.load_universe(
        selected_universe,
        current_only=True,
        version=version,
    )
    membership = (
        reader.load_membership(
            selected_universe,
            version=version,
        )
        if include_membership
        else None
    )
    manifest = reader.verify_version(version, require_price_semantics=True)
    contract = DataContract(
        schema_version=3,
        requested_universe=requested_universe,
        data_universe=selected_universe,
        dataset_version_id=version.version_id,
        dataset_run_id=version.run_id,
        target_session=version.target_session.isoformat(),
        bars_sha256=version.checksum_sha256,
        membership_sha256=version.membership_checksum_sha256,
        factor_publication_id=None,
        factor_generations={},
        runtime_factor_id=None,
        coverage=coverage.to_dict(),
        universe_sha256=version.universe_checksum_sha256,
        manifest_sha256=version.manifest_checksum_sha256,
        price_semantics=validate_price_semantics_contract(
            manifest.get("price_semantics")
        ),
    )
    return PublishedDailyDataBundle(
        version=version,
        bars=bars.reset_index(drop=True),
        universe=universe,
        membership=membership,
        contract=contract,
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
    version = reader.require_latest(universe, require_price_semantics=True)
    publication = validate_factor_research_publication(
        universe,
        version=version,
        factor_ids=factors,
    )
    try:
        benchmark = resolve_registered_benchmark_contract(
            universe,
            primary_version=version,
            reader=reader,
        ).to_dict()
    except BenchmarkDataError as exc:
        raise MarketDataNotReadyError(
            f"Cannot bind task benchmark: {exc}",
            data_universe=universe,
        ) from exc
    manifest = reader.verify_version(version, require_price_semantics=True)
    return DataContract(
        schema_version=3,
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
        universe_sha256=version.universe_checksum_sha256,
        manifest_sha256=version.manifest_checksum_sha256,
        price_semantics=validate_price_semantics_contract(
            manifest.get("price_semantics")
        ),
        benchmark=benchmark,
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
        # Version 3 requires an authenticated price-semantics publication.
        # Its distinct request key prevents an old successful v1/v2 request
        # from suppressing the mandatory immutable-history rebuild.
        "schema_version": 3,
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
    "PublishedDailyDataBundle",
    "PublishedDataBundle",
    "PublishedUniverseBundle",
    "current_named_contract",
    "enqueue_market_data_request",
    "inspect_coverage",
    "load_published_daily_data",
    "load_published_bundle",
    "load_published_universe",
    "resolve_published_version",
    "runtime_factor_id",
    "validate_daily_data_contract",
    "watchlist_universe_frame",
]
