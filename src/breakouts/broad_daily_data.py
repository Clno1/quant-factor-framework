"""Version-bound breakout adapter for broad coverage plus derived PIT membership."""
from __future__ import annotations

from dataclasses import fields
import inspect
from typing import Any, Callable, Iterable

import pandas as pd

from src.config import CONFIG
from src.data.access import (
    DataContract,
    DataCoverage,
    MarketDataNotReadyError,
    PublishedUniverseBundle,
    validate_daily_data_contract,
)
from src.data.broad_coverage import BroadCoverageReader
from src.data.foundation import DataFoundationError, DatasetVersion, MarketDataReader
from src.data.membership_state import resolve_membership_asof
from src.data.security_master_store import SecurityMasterStore
from src.data.universe_ids import US_EQUITY_COVERAGE, US_LIQUID_5M
from src.data.universe_publication import DerivedUniverseStore, DerivedUniverseVersion
from src.utils.market_calendar import (
    latest_publishable_xnys_session,
    xnys_session_on_or_before,
)


TickerSelector = Callable[[pd.DataFrame], Iterable[str]]


def _supports(callable_value: Any, parameter: str) -> bool:
    try:
        signature = inspect.signature(callable_value)
    except (TypeError, ValueError):
        return False
    return parameter in signature.parameters


def _require_parent(
    reader: MarketDataReader,
    version_id: str | None,
) -> DatasetVersion:
    kwargs: dict[str, Any] = {}
    if _supports(reader.require_latest, "require_price_semantics"):
        kwargs["require_price_semantics"] = True
    if _supports(reader.require_latest, "verify_partition_children"):
        kwargs["verify_partition_children"] = False
    if version_id:
        return reader.require_version(
            US_EQUITY_COVERAGE,
            version_id,
            **kwargs,
        )
    return reader.require_latest(US_EQUITY_COVERAGE, **kwargs)


def _verify_parent(
    reader: MarketDataReader,
    version: DatasetVersion,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if _supports(reader.verify_version, "require_price_semantics"):
        kwargs["require_price_semantics"] = True
    if _supports(reader.verify_version, "verify_partition_children"):
        kwargs["verify_partition_children"] = False
    return reader.verify_version(version, **kwargs)


def _universe_store(reader: MarketDataReader) -> DerivedUniverseStore:
    return DerivedUniverseStore(
        catalog=reader.catalog,
        snapshot_root=CONFIG.abs_path(
            str(CONFIG.data.broad_universe.snapshot_dir)
        ),
        market_reader=reader,
    )


def _expected_session(value: str | pd.Timestamp | None) -> pd.Timestamp:
    delay = int(getattr(CONFIG.data.foundation, "close_delay_minutes", 120))
    latest = latest_publishable_xnys_session(delay_minutes=delay)
    if value is None:
        return latest
    return min(latest, xnys_session_on_or_before(pd.Timestamp(value)))


def _current_metadata(
    *,
    reader: MarketDataReader,
    parent: DatasetVersion,
    universe_version: DerivedUniverseVersion,
    asof: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    store = _universe_store(reader)
    membership = store.load_membership(
        US_LIQUID_5M,
        version_id=universe_version.universe_version_id,
    )
    current = resolve_membership_asof(membership, asof)
    if current.empty:
        raise DataFoundationError(
            f"[{US_LIQUID_5M}] no active PIT members at {asof.date()}"
        )

    coverage = reader.load_universe(
        US_EQUITY_COVERAGE,
        current_only=False,
        version=parent,
    ).copy()
    coverage["security_id"] = coverage["security_id"].astype(str)
    coverage["ticker"] = coverage["ticker"].astype(str).str.upper()
    coverage = coverage.drop_duplicates("security_id", keep="last")

    member_columns = [
        "security_id",
        "ticker",
        "selection_price",
        "adv20_usd",
        "valid_sessions_20d",
        "reason_codes",
    ]
    current = current.loc[:, [c for c in member_columns if c in current.columns]].copy()
    current["security_id"] = current["security_id"].astype(str)
    current["ticker"] = current["ticker"].astype(str).str.upper()
    metadata = current.merge(
        coverage.drop(columns=["ticker"], errors="ignore"),
        on="security_id",
        how="left",
        validate="one_to_one",
    )
    if metadata["name"].isna().any():
        raise DataFoundationError(
            f"[{US_LIQUID_5M}] PIT members are missing Security Master metadata"
        )

    security_settings = CONFIG.data.security_master
    security_store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(security_settings.snapshot_dir)),
    )
    generation, frames = security_store.load_published()
    if (
        generation.generation_id != universe_version.security_master_generation_id
        or generation.manifest_sha256
        != universe_version.security_master_manifest_sha256
    ):
        raise DataFoundationError(
            f"[{US_LIQUID_5M}] PIT and Security Master generations differ"
        )
    classifications = frames["classifications"].copy()
    if not classifications.empty:
        starts = pd.to_datetime(classifications["effective_from"], errors="coerce")
        ends = pd.to_datetime(classifications["effective_to"], errors="coerce")
        known = pd.to_datetime(classifications["knowledge_date"], errors="coerce")
        classifications = classifications.loc[
            (starts.isna() | starts.le(asof))
            & (ends.isna() | ends.ge(asof))
            & known.notna()
            & known.le(asof)
        ].copy()
        classifications = (
            classifications.sort_values(
                ["security_id", "knowledge_date", "effective_from"],
                na_position="first",
            )
            .drop_duplicates("security_id", keep="last")
            .loc[:, ["security_id", "sector", "sub_industry"]]
        )
        metadata = metadata.merge(
            classifications,
            on="security_id",
            how="left",
            validate="one_to_one",
        )
    if "sector" not in metadata:
        metadata["sector"] = ""
    if "sub_industry" not in metadata:
        metadata["sub_industry"] = ""
    metadata["current_dollar_volume"] = pd.to_numeric(
        metadata.get("adv20_usd"), errors="coerce"
    )
    return metadata.sort_values("ticker").reset_index(drop=True), coverage


def _resolve_context(
    *,
    reader: MarketDataReader,
    dataset_version_id: str | None,
    end: str | pd.Timestamp | None,
) -> tuple[DatasetVersion, dict[str, Any], DerivedUniverseVersion, pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    expected = _expected_session(end)
    parent = _require_parent(reader, dataset_version_id)
    parent_manifest = _verify_parent(reader, parent)
    universe_store = _universe_store(reader)
    universe_version = universe_store.require_latest(US_LIQUID_5M)
    if universe_version.parent_dataset_version_id != parent.version_id:
        raise DataFoundationError(
            f"[{US_LIQUID_5M}] latest PIT publication is not bound to coverage "
            f"{parent.version_id}"
        )
    if parent.target_session < expected.date():
        raise DataFoundationError(
            f"[{US_EQUITY_COVERAGE}] target {parent.target_session} is stale; "
            f"expected {expected.date()}"
        )
    if universe_version.target_session < expected.date():
        raise DataFoundationError(
            f"[{US_LIQUID_5M}] PIT target {universe_version.target_session} is stale; "
            f"expected {expected.date()}"
        )
    metadata, coverage = _current_metadata(
        reader=reader,
        parent=parent,
        universe_version=universe_version,
        asof=expected,
    )
    return parent, parent_manifest, universe_version, metadata, coverage, expected


def load_broad_breakout_universe(
    *,
    dataset_version_id: str | None = None,
    end: str | pd.Timestamp | None = None,
    reader: MarketDataReader | None = None,
) -> PublishedUniverseBundle:
    """Return current PIT members while binding the parent coverage version."""
    reader = reader or MarketDataReader()
    parent, _manifest, _universe, metadata, _coverage, _expected = _resolve_context(
        reader=reader,
        dataset_version_id=dataset_version_id,
        end=end,
    )
    return PublishedUniverseBundle(version=parent, universe=metadata)


def _price_semantics(manifest: dict[str, Any]) -> dict[str, Any]:
    if "price_semantics" not in {item.name for item in fields(DataContract)}:
        return {}
    from src.data.price_semantics import validate_price_semantics_contract

    return validate_price_semantics_contract(manifest.get("price_semantics"))


def _contract(
    *,
    requested_universe: str,
    parent: DatasetVersion,
    parent_manifest: dict[str, Any],
    universe_version: DerivedUniverseVersion,
    coverage: DataCoverage,
) -> DataContract:
    derived = {
        "universe": US_LIQUID_5M,
        "universe_version_id": universe_version.universe_version_id,
        "parent_dataset_version_id": universe_version.parent_dataset_version_id,
        "target_session": universe_version.target_session.isoformat(),
        "membership_sha256": universe_version.membership_sha256,
        "eligibility_sha256": universe_version.eligibility_sha256,
        "manifest_sha256": universe_version.manifest_sha256,
        "security_master_generation_id": (
            universe_version.security_master_generation_id
        ),
        "security_master_manifest_sha256": (
            universe_version.security_master_manifest_sha256
        ),
    }
    coverage_payload = coverage.to_dict()
    coverage_payload["derived_universe"] = derived
    supported = {item.name for item in fields(DataContract)}
    values: dict[str, Any] = {
        "schema_version": 3 if "price_semantics" in supported else 2,
        "requested_universe": requested_universe,
        "data_universe": US_EQUITY_COVERAGE,
        "dataset_version_id": parent.version_id,
        "dataset_run_id": parent.run_id,
        "target_session": parent.target_session.isoformat(),
        "bars_sha256": parent.checksum_sha256,
        "membership_sha256": parent.membership_checksum_sha256,
        "factor_publication_id": None,
        "factor_generations": {},
        "runtime_factor_id": None,
        "coverage": coverage_payload,
        "universe_sha256": parent.universe_checksum_sha256,
        "manifest_sha256": parent.manifest_checksum_sha256,
    }
    if "price_semantics" in supported:
        values["price_semantics"] = _price_semantics(parent_manifest)
    return DataContract(**values)


def load_broad_breakout_daily_dataset(
    *,
    requested_universe: str,
    tickers: Iterable[str] | None = None,
    ticker_selector: TickerSelector | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    exact_universe: bool = False,
    dataset_version_id: str | None = None,
    min_latest_coverage: float | None = None,
    lookback_calendar_days: int = 400,
    reader: MarketDataReader | None = None,
):
    """Load PIT members from broad coverage without materializing a second lake."""
    from src.breakouts.daily_data import BreakoutDailyDataset, daily_frames_from_bars

    if tickers is not None and ticker_selector is not None:
        raise ValueError("tickers and ticker_selector are mutually exclusive")
    reader = reader or MarketDataReader()
    (
        parent,
        parent_manifest,
        universe_version,
        member_metadata,
        coverage_metadata,
        expected,
    ) = _resolve_context(
        reader=reader,
        dataset_version_id=dataset_version_id,
        end=end,
    )

    requested = (
        [str(value).strip().upper() for value in ticker_selector(member_metadata.copy())]
        if ticker_selector is not None
        else [str(value).strip().upper() for value in tickers or []]
    )
    requested = list(dict.fromkeys(value for value in requested if value))
    if not requested:
        requested = member_metadata["ticker"].astype(str).tolist()

    metadata = member_metadata.loc[
        member_metadata["ticker"].isin(requested)
    ].copy()
    missing_members = sorted(set(requested) - set(metadata["ticker"]))
    if missing_members:
        support = coverage_metadata.loc[
            coverage_metadata["ticker"].isin(missing_members)
            & coverage_metadata["asset_type"].astype(str).str.upper().eq("ETF")
            & coverage_metadata["is_current_coverage"].fillna(False).astype(bool)
        ].copy()
        support["selection_price"] = pd.NA
        support["adv20_usd"] = pd.NA
        support["valid_sessions_20d"] = pd.NA
        support["reason_codes"] = "EXPLICIT_BENCHMARK_SUPPORT"
        support["sector"] = ""
        support["sub_industry"] = ""
        support["current_dollar_volume"] = pd.NA
        metadata = pd.concat([metadata, support], ignore_index=True, sort=False)
    unresolved = sorted(set(requested) - set(metadata["ticker"].astype(str)))
    if unresolved:
        raise DataFoundationError(
            f"[{US_LIQUID_5M}] requested securities are not current PIT members "
            f"or explicit ETF benchmarks: {unresolved[:20]}"
        )
    metadata = (
        metadata.drop_duplicates("ticker", keep="last")
        .set_index("ticker")
        .loc[requested]
        .reset_index()
    )

    effective_start = (
        pd.Timestamp(start).normalize()
        if start is not None
        else expected - pd.Timedelta(days=int(lookback_calendar_days))
    )
    security_ids = metadata["security_id"].astype(str).tolist()
    bars = BroadCoverageReader(market_reader=reader).load_bars(
        security_ids=security_ids,
        start=effective_start,
        end=expected,
        version=parent,
        columns=[
            "date",
            "security_id",
            "ticker",
            "open",
            "high",
            "low",
            "close",
            "adj_close",
            "volume",
        ],
    )
    ticker_by_id = metadata.set_index("security_id")["ticker"].astype(str).to_dict()
    bars["ticker"] = bars["security_id"].astype(str).map(ticker_by_id)
    bars = bars.loc[bars["ticker"].notna()].copy()

    observed = sorted(set(bars["ticker"].astype(str)))
    latest_rows = bars.loc[pd.to_datetime(bars["date"]).eq(expected)]
    latest_observed = set(latest_rows["ticker"].astype(str))
    latest_coverage = len(latest_observed & set(requested)) / len(requested)
    threshold = float(min_latest_coverage if min_latest_coverage is not None else 0.98)
    missing = sorted(set(requested) - set(observed))
    failures: list[str] = []
    if bars.empty:
        failures.append("no_bars")
    if missing and threshold >= 1.0:
        failures.append("missing_tickers")
    if latest_coverage < threshold:
        failures.append("latest_session_coverage")
    if exact_universe and set(metadata["ticker"]) != set(requested):
        failures.append("unexpected_current_members")
    coverage = DataCoverage(
        data_universe=US_EQUITY_COVERAGE,
        requested_tickers=tuple(requested),
        observed_tickers=tuple(observed),
        missing_tickers=tuple(missing),
        unexpected_tickers=(),
        requested_start=effective_start.date().isoformat(),
        requested_end=expected.date().isoformat(),
        required_history_start=None,
        membership_start=None,
        expected_session=expected.date().isoformat(),
        observed_session=(
            pd.Timestamp(bars["date"].max()).date().isoformat()
            if not bars.empty
            else None
        ),
        latest_coverage=latest_coverage,
        open_coverage=(
            float(pd.to_numeric(bars["open"], errors="coerce").notna().mean())
            if not bars.empty
            else 0.0
        ),
        min_date_by_ticker={
            str(key): pd.Timestamp(value).date().isoformat()
            for key, value in bars.groupby("ticker")["date"].min().items()
        },
        max_date_by_ticker={
            str(key): pd.Timestamp(value).date().isoformat()
            for key, value in bars.groupby("ticker")["date"].max().items()
        },
        passed=not failures,
        failures=tuple(failures),
    )
    if failures:
        raise MarketDataNotReadyError(
            f"[{US_LIQUID_5M}] broad breakout contract failed: {failures}",
            data_universe=US_EQUITY_COVERAGE,
            coverage=coverage,
        )
    contract = _contract(
        requested_universe=requested_universe,
        parent=parent,
        parent_manifest=parent_manifest,
        universe_version=universe_version,
        coverage=coverage,
    )
    return BreakoutDailyDataset(
        requested_universe=requested_universe,
        data_universe=US_LIQUID_5M,
        version=parent,
        contract=contract,
        universe=metadata,
        frames=daily_frames_from_bars(bars),
    )


def validate_breakout_daily_data_contract(
    contract: DataContract | dict[str, Any],
) -> DatasetVersion:
    """Authenticate both the parent bars and the exact derived PIT publication."""
    payload = contract.to_dict() if isinstance(contract, DataContract) else dict(contract)
    parent = validate_daily_data_contract(payload)
    derived = (payload.get("coverage") or {}).get("derived_universe")
    if not isinstance(derived, dict):
        return parent
    if derived.get("universe") != US_LIQUID_5M:
        raise DataFoundationError("breakout derived-universe contract is invalid")
    reader = MarketDataReader()
    store = _universe_store(reader)
    version = store.get(US_LIQUID_5M, str(derived.get("universe_version_id") or ""))
    if version is None:
        raise DataFoundationError("breakout PIT universe version is missing")
    store.verify(version)
    expected = {
        "parent_dataset_version_id": parent.version_id,
        "target_session": version.target_session.isoformat(),
        "membership_sha256": version.membership_sha256,
        "eligibility_sha256": version.eligibility_sha256,
        "manifest_sha256": version.manifest_sha256,
        "security_master_generation_id": version.security_master_generation_id,
        "security_master_manifest_sha256": (
            version.security_master_manifest_sha256
        ),
    }
    mismatches = [
        key for key, value in expected.items() if str(derived.get(key)) != str(value)
    ]
    if mismatches:
        raise DataFoundationError(
            f"breakout PIT universe contract mismatch: {mismatches}"
        )
    return parent


__all__ = [
    "load_broad_breakout_daily_dataset",
    "load_broad_breakout_universe",
    "validate_breakout_daily_data_contract",
]
