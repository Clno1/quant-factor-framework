from __future__ import annotations

import pandas as pd
import pytest

import src.backtest.runner as runner_module
from src.backtest.integrity import quintile_backtest_integrity
from src.data.access import DataContract
import src.data.integrity as data_integrity
from src.research_universes import research_universe_registry


def _contract(version_id: str = "TEST_VERSION") -> DataContract:
    return DataContract(
        schema_version=2,
        requested_universe="SP500",
        data_universe="SP500",
        dataset_version_id=version_id,
        dataset_run_id="run-1",
        target_session="2026-08-21",
        bars_sha256="bars",
        membership_sha256="membership",
        factor_publication_id=None,
        factor_generations={},
        runtime_factor_id=None,
        coverage={},
        universe_sha256="universe",
        manifest_sha256="manifest",
    )


def test_registry_benchmarks_are_explicit() -> None:
    registry = research_universe_registry()
    assert registry.get("SP500").benchmark == "SPY"
    assert registry.get("NASDAQ100").benchmark == "QQQ"


def test_data_contract_to_dict_includes_bound_benchmark_publication() -> None:
    version_id = "TEST_VERSION"
    key = ("SP500", version_id)
    benchmark = {
        "schema_version": 1,
        "ticker": "SPY",
        "data_universe": "US_EQUITY_COVERAGE",
        "dataset_version_id": "coverage-v1",
        "dataset_run_id": "coverage-run",
        "target_session": "2026-08-21",
        "bars_sha256": "coverage-bars",
        "manifest_sha256": "coverage-manifest",
        "source": "US_EQUITY_COVERAGE",
    }
    with data_integrity._LOCK:
        previous = data_integrity._BENCHMARK_CONTRACTS.get(key)
        data_integrity._BENCHMARK_CONTRACTS[key] = benchmark
    try:
        payload = _contract(version_id).to_dict()
    finally:
        with data_integrity._LOCK:
            if previous is None:
                data_integrity._BENCHMARK_CONTRACTS.pop(key, None)
            else:
                data_integrity._BENCHMARK_CONTRACTS[key] = previous
    assert payload["benchmark"]["ticker"] == "SPY"
    assert payload["benchmark"]["dataset_version_id"] == "coverage-v1"
    assert payload["benchmark"]["bars_sha256"] == "coverage-bars"


def test_async_runner_is_routed_to_integrity_backtest() -> None:
    assert runner_module.quintile_backtest is quintile_backtest_integrity


def test_formal_semantic_backtest_cannot_fall_back_to_equal_weight_benchmark() -> None:
    dates = pd.date_range("2024-01-02", periods=4, freq="B")
    factor = pd.DataFrame(
        {"AAA": [1.0, 1.0, 1.0, 1.0], "BBB": [2.0, 2.0, 2.0, 2.0]},
        index=dates,
    )
    returns = pd.DataFrame(0.0, index=dates, columns=factor.columns)
    execution_open = pd.DataFrame(100.0, index=dates, columns=factor.columns)
    execution_close = pd.DataFrame(100.0, index=dates, columns=factor.columns)
    adjusted_close = pd.DataFrame(90.0, index=dates, columns=factor.columns)
    adjusted_close.attrs["execution_close"] = execution_close
    adjusted_close.attrs["total_return_open"] = pd.DataFrame(
        90.0, index=dates, columns=factor.columns
    )
    adjusted_close.attrs["benchmark_error"] = "SPY publication missing"
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=factor.columns)

    with pytest.raises(ValueError, match="immutable registered benchmark"):
        quintile_backtest_integrity(
            factor,
            returns,
            n_groups=2,
            rebalance_days=1,
            open_df=execution_open,
            price_df=adjusted_close,
            volume_df=volume,
            benchmark_returns=None,
        )
