"""Compatibility adapter that upgrades published wide tables in one boundary.

The project has many mature consumers of ``MarketDataReader.load_wide_tables``.
Rather than duplicating those consumers while migrating price/exposure semantics,
this adapter decorates the reader once at package import time. It adds explicit
semantic matrices and auditable temporal-policy metadata while preserving the
legacy keys until callers have migrated.
"""
from __future__ import annotations

from threading import RLock
from typing import Any

import pandas as pd

from src.data.benchmark import (
    BenchmarkDataError,
    load_registered_benchmark,
    resolve_registered_benchmark_contract,
)
from src.data.foundation import MarketDataReader
from src.data.price_semantics import PriceSemantics
from src.research_universes.registry import ResearchUniverseRegistryError


# DataContract lives in access.py; importing it at module import time would
# create a circular dependency because access imports foundation. It is patched
# lazily in ``install_data_contract_benchmark_adapter``.
_LOCK = RLock()
_BENCHMARK_CONTRACTS: dict[tuple[str, str], dict[str, Any]] = {}
_INSTALLED = False
_ORIGINAL_LOAD_WIDE = None
_ORIGINAL_CONTRACT_TO_DICT = None


def _single_policy(metadata: pd.DataFrame, column: str) -> str | None:
    if column not in metadata.columns:
        return None
    values = sorted(
        set(metadata[column].dropna().astype(str).str.strip().str.upper()) - {""}
    )
    if len(values) == 1:
        return values[0]
    return "MIXED" if values else None


def _cache_benchmark_contract(
    requested_universe: str,
    primary_version_id: str,
    contract: dict[str, Any],
) -> None:
    key = (str(requested_universe).upper(), str(primary_version_id))
    with _LOCK:
        _BENCHMARK_CONTRACTS[key] = dict(contract)


def _decorate_wide(
    reader: MarketDataReader,
    universe: str,
    wide: dict[str, pd.DataFrame],
    *,
    selected_version,
    start,
    end,
) -> dict[str, pd.DataFrame]:
    semantics = PriceSemantics.from_wide(wide)
    wide["execution_open"] = semantics.execution_open
    wide["execution_close"] = semantics.execution_close
    wide["total_return_open"] = semantics.total_return_open
    wide["total_return_close"] = semantics.total_return_close
    wide["total_return_returns"] = semantics.total_returns
    wide["returns"] = semantics.total_returns

    metadata = reader.load_universe(
        universe,
        current_only=False,
        version=selected_version,
    )
    classification_policy = _single_policy(metadata, "classification_policy")
    market_cap_policy = _single_policy(metadata, "market_cap_policy")
    if "sector" in wide:
        wide["sector"].attrs["classification_policy"] = classification_policy
    if "market_cap" in wide:
        wide["market_cap"].attrs["market_cap_policy"] = market_cap_policy

    legacy_price = wide.get("adj_close")
    if legacy_price is not None:
        legacy_price.attrs["execution_close"] = semantics.execution_close
        legacy_price.attrs["total_return_open"] = semantics.total_return_open
        legacy_price.attrs["total_return_close"] = semantics.total_return_close
        legacy_price.attrs["price_semantics"] = "EXPLICIT_EXECUTION_AND_TOTAL_RETURN_V1"

    try:
        benchmark = load_registered_benchmark(
            universe,
            start=start,
            end=end,
            primary_version=selected_version,
            reader=reader,
        )
    except ResearchUniverseRegistryError:
        # Coverage/watchlist universes are data containers, not named research
        # universes and therefore have no registry benchmark by design.
        return wide
    except BenchmarkDataError as exc:
        if legacy_price is not None:
            legacy_price.attrs["benchmark_error"] = str(exc)
        return wide

    contract = benchmark.contract.to_dict()
    _cache_benchmark_contract(universe, selected_version.version_id, contract)
    if legacy_price is not None:
        legacy_price.attrs["benchmark_returns"] = benchmark.holding_returns
        legacy_price.attrs["benchmark_contract"] = contract
    semantics.total_returns.attrs["benchmark_returns"] = benchmark.holding_returns
    semantics.total_returns.attrs["benchmark_contract"] = contract
    wide["returns"] = semantics.total_returns
    wide["total_return_returns"] = semantics.total_returns
    return wide


def install_data_integrity_adapter() -> None:
    """Install the reader compatibility bridge exactly once."""
    global _INSTALLED, _ORIGINAL_LOAD_WIDE
    with _LOCK:
        if _INSTALLED:
            return
        _ORIGINAL_LOAD_WIDE = MarketDataReader.load_wide_tables

        def load_wide_tables_integrity(self, universe: str, **kwargs):
            selected = self._resolve_version(universe, kwargs.get("version"))
            call_kwargs = dict(kwargs)
            call_kwargs["version"] = selected
            wide = _ORIGINAL_LOAD_WIDE(self, universe, **call_kwargs)
            return _decorate_wide(
                self,
                universe,
                wide,
                selected_version=selected,
                start=kwargs.get("start"),
                end=kwargs.get("end"),
            )

        MarketDataReader.load_wide_tables = load_wide_tables_integrity
        _INSTALLED = True


def install_data_contract_benchmark_adapter() -> None:
    """Expose immutable benchmark identity in every named-universe DataContract."""
    global _ORIGINAL_CONTRACT_TO_DICT
    from src.data.access import DataContract

    with _LOCK:
        if getattr(DataContract, "_benchmark_integrity_adapter", False):
            return
        _ORIGINAL_CONTRACT_TO_DICT = DataContract.to_dict

        def to_dict_with_benchmark(self):
            payload = _ORIGINAL_CONTRACT_TO_DICT(self)
            key = (str(self.requested_universe).upper(), self.dataset_version_id)
            with _LOCK:
                benchmark = _BENCHMARK_CONTRACTS.get(key)

            if benchmark is None:
                # Task creation can serialize a contract before wide tables are
                # loaded. Resolve only immutable metadata here so SPY/QQQ is
                # frozen from the beginning, not appended merely at task end.
                reader = MarketDataReader()
                try:
                    primary = reader.require_version(
                        str(self.data_universe).upper(),
                        self.dataset_version_id,
                    )
                    resolved = resolve_registered_benchmark_contract(
                        self.requested_universe,
                        primary_version=primary,
                        reader=reader,
                    ).to_dict()
                except ResearchUniverseRegistryError:
                    resolved = None
                except BenchmarkDataError as exc:
                    payload["benchmark_error"] = str(exc)
                    resolved = None
                if resolved is not None:
                    _cache_benchmark_contract(
                        self.requested_universe,
                        self.dataset_version_id,
                        resolved,
                    )
                    benchmark = resolved

            if benchmark is not None:
                payload["benchmark"] = dict(benchmark)
            return payload

        DataContract.to_dict = to_dict_with_benchmark
        DataContract._benchmark_integrity_adapter = True


__all__ = [
    "install_data_contract_benchmark_adapter",
    "install_data_integrity_adapter",
]
