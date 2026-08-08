"""Orchestration for the first market turning-point research dataset."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from src.config import PROJECT_ROOT
from src.data.foundation import DatasetVersion, MarketDataReader
from src.data.pit import build_membership_mask, find_membership_file
from src.market_regime_research import SCHEMA_VERSION
from src.market_regime_research.artifacts import file_sha256, publish_research_run
from src.market_regime_research.features import (
    combine_feature_bundles,
    compute_breadth_features,
    compute_credit_features,
    compute_cross_asset_features,
    compute_momentum_stress_features,
    compute_price_features,
    compute_volatility_features,
)
from src.market_regime_research.labels import build_turning_point_labels
from src.market_regime_research.models import (
    DataContractError,
    FeatureBundle,
    ResearchRunResult,
)
from src.market_regime_research.pit import membership_metadata_path
from src.market_regime_research.settings import MarketRegimeResearchSettings
from src.market_regime_research.sources import (
    load_prepared_credit,
    load_prepared_prices,
    load_prepared_volatility,
    price_path,
)
from src.utils.identifiers import safe_path_component


def _primary_index(
    prices: Mapping[str, pd.DataFrame],
    primary_symbol: str,
) -> pd.DatetimeIndex:
    if primary_symbol not in prices:
        raise DataContractError(f"Primary market series is missing: {primary_symbol}")
    index = pd.DatetimeIndex(pd.to_datetime(prices[primary_symbol].index))
    if index.tz is not None:
        index = index.tz_convert(None)
    index = index.normalize()
    if index.empty or index.has_duplicates:
        raise DataContractError("Primary market series has an invalid date index")
    return index.sort_values()


def _align_available_bundle(
    bundle: FeatureBundle,
    target_index: pd.DatetimeIndex,
    *,
    max_forward_fill_rows: int = 5,
) -> FeatureBundle:
    """Carry recent available data forward without hiding a stale source."""
    union = bundle.values.index.union(target_index).sort_values()
    values = (
        bundle.values.reindex(union)
        .ffill(limit=int(max_forward_fill_rows))
        .reindex(target_index)
    )
    values.index.name = "date"
    return FeatureBundle(
        values=values,
        registry=bundle.registry,
        diagnostics=bundle.diagnostics,
    )


def _validate_wide_contract(wide_tables: Mapping[str, pd.DataFrame]) -> None:
    required = {"adj_close", "high", "low", "volume"}
    missing = required - set(wide_tables)
    if missing:
        raise DataContractError(
            "SP500 wide tables are missing Stage-A fields "
            f"{sorted(missing)}. Rebuild processed data to create high/low."
        )
    reference = wide_tables["adj_close"]
    for name in ("high", "low", "volume"):
        frame = wide_tables[name]
        if not reference.index.equals(frame.index) or not reference.columns.equals(
            frame.columns
        ):
            raise DataContractError(
                f"SP500 {name} matrix does not align with adj_close"
            )


def build_research_dataset(
    *,
    settings: MarketRegimeResearchSettings,
    prices: Mapping[str, pd.DataFrame],
    volatility: pd.DataFrame,
    credit: pd.DataFrame | None = None,
    adj_close: pd.DataFrame | None = None,
    membership_mask: pd.DataFrame | None = None,
) -> tuple[FeatureBundle, pd.DataFrame, dict[str, Any]]:
    """
    Pure computation boundary used by both the CLI and deterministic tests.

    Omitting ``adj_close`` and ``membership_mask`` creates the market-only core
    dataset.  Providing exactly one is rejected because breadth must never fall
    back to a static/current universe.
    """
    primary_index = _primary_index(prices, settings.primary_symbol)
    bundles = [
        compute_price_features(prices, settings.features),
        compute_volatility_features(volatility),
        compute_cross_asset_features(prices),
    ]
    if credit is not None:
        bundles.append(
            _align_available_bundle(
                compute_credit_features(credit),
                primary_index,
            )
        )

    if (adj_close is None) != (membership_mask is None):
        raise DataContractError(
            "adj_close and membership_mask must be supplied together"
        )
    if adj_close is not None and membership_mask is not None:
        spy = prices.get("SPY")
        if spy is None:
            raise DataContractError("SPY is required as the cap-weight breadth proxy")
        spy_close_column = (
            "adj_close" if "adj_close" in spy.columns else "close"
        )
        bundles.extend(
            [
                compute_breadth_features(
                    adj_close,
                    membership_mask,
                    benchmark_close=spy[spy_close_column],
                    settings=settings.features,
                ),
                compute_momentum_stress_features(
                    adj_close,
                    membership_mask,
                    settings.features,
                ),
            ]
        )

    combined = combine_feature_bundles(*bundles)
    combined.values = combined.values.replace([np.inf, -np.inf], np.nan)
    combined.values = combined.values.reindex(primary_index)
    combined.values.index.name = "date"
    all_null = combined.values.columns[combined.values.isna().all()].tolist()
    if all_null:
        raise DataContractError(
            f"Feature columns are entirely null: {all_null}"
        )

    labels = build_turning_point_labels(
        prices[settings.primary_symbol],
        settings.labels,
    ).reindex(primary_index)
    minimum_rows = (
        settings.labels.minimum_history + max(settings.labels.horizons) + 1
    )
    if len(labels) < minimum_rows:
        raise DataContractError(
            f"Primary history has {len(labels)} rows; at least {minimum_rows} required"
        )

    diagnostics: dict[str, Any] = {
        "mode": "full_pit" if adj_close is not None else "market_core_only",
        "date_range": {
            "start": primary_index.min().date().isoformat(),
            "end": primary_index.max().date().isoformat(),
            "sessions": len(primary_index),
        },
        "features": {
            "columns": len(combined.values.columns),
            "minimum_non_null_coverage": float(
                combined.values.notna().mean().min()
            ),
            "median_non_null_coverage": float(
                combined.values.notna().mean().median()
            ),
        },
        "labels": {},
    }
    for horizon in settings.labels.horizons:
        for side in ("top", "bottom"):
            column = f"{side}_label_{horizon}d"
            values = labels[column]
            diagnostics["labels"][column] = {
                "eligible": int(values.notna().sum()),
                "positive": int((values == 1).sum()),
                "negative": int((values == 0).sum()),
                "ambiguous": int(
                    (
                        labels[f"{side}_first_touch_{horizon}d"]
                        == "ambiguous"
                    ).sum()
                ),
            }
    return combined, labels, diagnostics


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Source manifest missing: {path}. Run prepare first."
        ) from exc
    if not isinstance(value, dict):
        raise DataContractError("Source manifest must contain a JSON object")
    return value


def _published_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _plain_sha256(value: Any) -> str:
    return str(value or "").removeprefix("sha256:")


def _validate_source_manifest(
    settings: MarketRegimeResearchSettings,
    manifest: Mapping[str, Any],
    *,
    include_credit: bool,
    expected_end: pd.Timestamp,
) -> None:
    """Bind prepared files to the exact PASS manifest consumed by this run."""
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DataContractError(
            "Source manifest schema_version does not match the research code"
        )
    if manifest.get("configured_end") != expected_end.date().isoformat():
        raise DataContractError(
            "Source manifest end date does not match prepared primary prices; "
            "rerun prepare"
        )
    if include_credit and manifest.get("credit_included") is not True:
        raise DataContractError(
            "Credit was requested but the source manifest excludes it; rerun "
            "prepare without --skip-credit"
        )
    sources = manifest.get("sources")
    if not isinstance(sources, list):
        raise DataContractError("Source manifest sources must be a list")

    path_entries: dict[str, list[Mapping[str, Any]]] = {}
    for entry in sources:
        if not isinstance(entry, Mapping):
            raise DataContractError("Source manifest contains a non-object entry")
        path_value = entry.get("path")
        if path_value is not None and entry.get("file_sha256") is not None:
            path_entries.setdefault(str(path_value), []).append(entry)

    required_paths = [
        price_path(settings, instrument.symbol)
        for instrument in settings.instruments
    ]
    required_paths.append(settings.volatility_path)
    if include_credit:
        required_paths.append(settings.credit_path)

    raw_root = settings.raw_root.resolve()
    for path in required_paths:
        resolved = Path(path).resolve()
        try:
            relative = resolved.relative_to(raw_root).as_posix()
        except ValueError as exc:
            raise DataContractError(
                f"Prepared source escapes raw_root: {path}"
            ) from exc
        entries = path_entries.get(relative, [])
        if len(entries) != 1:
            raise DataContractError(
                f"Source manifest must contain exactly one entry for {relative}"
            )
        entry = entries[0]
        if entry.get("quality_status") != "PASS":
            raise DataContractError(
                f"Source manifest entry is not PASS: {relative}"
            )
        expected_hash = entry.get("file_sha256")
        actual_hash = file_sha256(resolved)
        if expected_hash != actual_hash:
            raise DataContractError(
                f"Prepared source hash differs from manifest for {relative}; "
                "rerun prepare"
            )


def _load_validated_pit_metadata(
    membership_path: Path,
    *,
    expected_asof: pd.Timestamp,
) -> dict[str, Any]:
    metadata_path = membership_metadata_path(membership_path)
    metadata = _read_json_object(metadata_path)
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise DataContractError("PIT metadata schema_version is incompatible")
    if metadata.get("quality_status") != "PASS" or metadata.get("strict") is not True:
        raise DataContractError(
            "Production PIT metadata must prove a strict PASS reconstruction"
        )
    try:
        metadata_asof = pd.Timestamp(metadata.get("asof")).normalize()
    except Exception as exc:  # noqa: BLE001
        raise DataContractError("PIT metadata contains an invalid asof date") from exc
    if pd.isna(metadata_asof):
        raise DataContractError("PIT metadata contains an invalid asof date")
    if metadata_asof.tzinfo is not None:
        metadata_asof = metadata_asof.tz_localize(None)
    if metadata_asof < expected_asof.normalize():
        raise DataContractError(
            "PIT snapshot asof predates the primary research end date"
        )
    if metadata.get("membership_sha256") != file_sha256(membership_path):
        raise DataContractError(
            "PIT membership hash differs from its publication metadata"
        )
    diagnostics = metadata.get("diagnostics")
    if not isinstance(diagnostics, Mapping):
        raise DataContractError("PIT metadata diagnostics must be an object")
    if (
        diagnostics.get("quality_status") != "PASS"
        or diagnostics.get("inconsistency_count") != 0
    ):
        raise DataContractError("PIT metadata diagnostics are not clean")
    return dict(metadata)


def _input_manifest(
    settings: MarketRegimeResearchSettings,
    *,
    source_manifest: Mapping[str, Any],
    full_pit: bool,
    include_credit: bool,
    pit_diagnostics: Mapping[str, Any] | None,
    pit_publication: Mapping[str, Any] | None,
    market_version: DatasetVersion | None,
) -> dict[str, Any]:
    inputs: list[dict[str, Any]] = []
    for instrument in settings.instruments:
        path = price_path(settings, instrument.symbol)
        inputs.append(
            {
                "name": f"price:{instrument.symbol}",
                "sha256": file_sha256(path),
                "rows": len(pd.read_parquet(path, columns=["close"])),
            }
        )
    inputs.append(
        {
            "name": "cboe_volatility",
            "sha256": file_sha256(settings.volatility_path),
        }
    )
    if include_credit:
        inputs.append(
            {
                "name": "fred_credit",
                "sha256": file_sha256(settings.credit_path),
            }
        )
    if full_pit:
        if market_version is None:
            raise DataContractError(
                "Full PIT research has no bound market-data version"
            )
        published_inputs = {
            "bars": market_version.bars_path,
            "universe": market_version.universe_path,
            "membership": market_version.membership_path,
            "manifest": market_version.manifest_path,
        }
        for name, path_value in published_inputs.items():
            if path_value is None:
                raise DataContractError(
                    f"Published {settings.pit.universe} version has no {name} file"
                )
            path = _published_path(path_value)
            inputs.append(
                {
                    "name": f"{settings.pit.universe}:{name}",
                    "sha256": file_sha256(path),
                }
            )
        membership_path = find_membership_file(settings.pit.universe)
        if membership_path is None:
            raise FileNotFoundError(
                f"No PIT membership file found for {settings.pit.universe}"
            )
        inputs.append(
            {
                "name": f"{settings.pit.universe}:membership",
                "sha256": file_sha256(membership_path),
            }
        )
        metadata_path = membership_metadata_path(membership_path)
        inputs.append(
            {
                "name": f"{settings.pit.universe}:membership_metadata",
                "sha256": file_sha256(metadata_path),
            }
        )
    return {
        "source_manifest": dict(source_manifest),
        "files": inputs,
        "point_in_time": dict(pit_diagnostics or {}),
        "point_in_time_publication": dict(pit_publication or {}),
        "market_data_version": (
            {
                "version_id": market_version.version_id,
                "run_id": market_version.run_id,
                "universe": market_version.universe,
                "target_session": market_version.target_session.isoformat(),
                "bars_sha256": market_version.checksum_sha256,
                "membership_sha256": market_version.membership_checksum_sha256,
            }
            if market_version is not None
            else None
        ),
    }


def run_market_regime_research(
    settings: MarketRegimeResearchSettings,
    *,
    core_only: bool = False,
    include_credit: bool = True,
    run_id: str | None = None,
) -> ResearchRunResult:
    """Load prepared inputs, enforce PIT gates, calculate, and publish."""
    if not settings.enabled:
        raise RuntimeError("market_regime_research is disabled in configuration")
    prices = load_prepared_prices(settings)
    primary_index = _primary_index(prices, settings.primary_symbol)
    volatility = load_prepared_volatility(settings)
    credit: pd.DataFrame | None = None
    if include_credit:
        credit = load_prepared_credit(settings)
    source_manifest = _read_json_object(settings.source_manifest_path)
    _validate_source_manifest(
        settings,
        source_manifest,
        include_credit=include_credit,
        expected_end=primary_index.max(),
    )

    wide_tables: dict[str, pd.DataFrame] | None = None
    membership_mask: pd.DataFrame | None = None
    pit_diagnostics: dict[str, Any] | None = None
    pit_publication: dict[str, Any] | None = None
    market_version: DatasetVersion | None = None
    if not core_only:
        reader = MarketDataReader()
        market_version = reader.require_latest(settings.pit.universe)
        wide_tables = reader.load_wide_tables(
            settings.pit.universe,
            version=market_version,
        )
        _validate_wide_contract(wide_tables)
        published_membership = reader.load_membership(
            settings.pit.universe,
            version=market_version,
        )
        if published_membership is None:
            raise DataContractError(
                f"Published {settings.pit.universe} version has no PIT membership"
            )
        membership_path = find_membership_file(settings.pit.universe)
        if membership_path is None:
            raise FileNotFoundError(
                f"No PIT membership file found for {settings.pit.universe}"
            )
        pit_publication = _load_validated_pit_metadata(
            membership_path,
            expected_asof=primary_index.max(),
        )
        version_manifest = _read_json_object(
            _published_path(market_version.manifest_path)
        )
        source_hash = _plain_sha256(pit_publication.get("membership_sha256"))
        version_source_hash = _plain_sha256(
            version_manifest.get("pit_membership_source_sha256")
        )
        if not source_hash or version_source_hash != source_hash:
            raise DataContractError(
                "Published market-data version is not bound to the validated "
                "PIT membership source; republish market data"
            )
        membership_mask, diagnostics = build_membership_mask(
            wide_tables["adj_close"].index,
            wide_tables["adj_close"].columns,
            settings.pit.universe,
            required=True,
            membership_override=published_membership,
            membership_source=market_version.membership_path,
            membership_source_sha256=(
                market_version.membership_checksum_sha256
            ),
        )
        if membership_mask is None:
            raise DataContractError("PIT membership unexpectedly returned no mask")
        pit_diagnostics = diagnostics.to_dict()

    features, labels, diagnostics = build_research_dataset(
        settings=settings,
        prices=prices,
        volatility=volatility,
        credit=credit,
        adj_close=(
            wide_tables["adj_close"] if wide_tables is not None else None
        ),
        membership_mask=membership_mask,
    )
    manifest = _input_manifest(
        settings,
        source_manifest=source_manifest,
        full_pit=not core_only,
        include_credit=include_credit,
        pit_diagnostics=pit_diagnostics,
        pit_publication=pit_publication,
        market_version=market_version,
    )
    return publish_research_run(
        output_root=settings.output_root,
        features=features,
        labels=labels,
        input_manifest=manifest,
        diagnostics=diagnostics,
        run_id=run_id,
    )


__all__ = [
    "build_research_dataset",
    "run_market_regime_research",
]
