from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import src.backtest.runner as runner_module
from src.backtest.quintile_v2 import quintile_backtest_v2
from src.data.access import DataContract, _resolve_bundle_benchmark
from src.data.price_semantics import PriceSemantics, build_price_semantics_contract
from src.research_universes import research_universe_registry


def _contract(
    version_id: str = "TEST_VERSION",
    *,
    benchmark: dict | None = None,
) -> DataContract:
    return DataContract(
        schema_version=3,
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
        price_semantics=build_price_semantics_contract(
            source="TEST_CANONICAL_FIXTURE",
            history_mode="FULL_REBUILD",
        ),
        benchmark=benchmark,
    )


def test_registry_benchmarks_are_explicit() -> None:
    registry = research_universe_registry()
    assert registry.get("SP500").benchmark == "SPY"
    assert registry.get("NASDAQ100").benchmark == "QQQ"


def test_data_contract_to_dict_includes_bound_benchmark_publication() -> None:
    version_id = "TEST_VERSION"
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
    payload = _contract(version_id, benchmark=benchmark).to_dict()
    assert payload["benchmark"]["ticker"] == "SPY"
    assert payload["benchmark"]["dataset_version_id"] == "coverage-v1"
    assert payload["benchmark"]["bars_sha256"] == "coverage-bars"


def test_async_runner_uses_explicit_v2_backtest() -> None:
    assert runner_module.quintile_backtest_v2 is quintile_backtest_v2


def test_formal_semantic_backtest_cannot_fall_back_to_equal_weight_benchmark() -> None:
    dates = pd.date_range("2024-01-02", periods=4, freq="B")
    factor = pd.DataFrame(
        {"AAA": [1.0, 1.0, 1.0, 1.0], "BBB": [2.0, 2.0, 2.0, 2.0]},
        index=dates,
    )
    returns = pd.DataFrame(0.0, index=dates, columns=factor.columns)
    execution_open = pd.DataFrame(100.0, index=dates, columns=factor.columns)
    execution_close = pd.DataFrame(100.0, index=dates, columns=factor.columns)
    total_return_close = pd.DataFrame(90.0, index=dates, columns=factor.columns)
    total_return_open = pd.DataFrame(
        90.0, index=dates, columns=factor.columns
    )
    volume = pd.DataFrame(1_000_000.0, index=dates, columns=factor.columns)

    with pytest.raises(ValueError, match="explicit immutable benchmark"):
        quintile_backtest_v2(
            factor,
            returns,
            n_groups=2,
            rebalance_days=1,
            execution_open_df=execution_open,
            execution_close_df=execution_close,
            total_return_open_df=total_return_open,
            total_return_close_df=total_return_close,
            volume_df=volume,
            benchmark_returns=None,
        )


def test_unregistered_watchlist_uses_explicit_total_return_basket_benchmark() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    columns = ["AAA", "BBB"]
    wide = {
        "open": pd.DataFrame(
            [[100.0, 50.0], [100.0, 50.0], [100.0, 50.0]],
            index=dates,
            columns=columns,
        ),
        "close": pd.DataFrame(
            [[100.0, 50.0], [100.0, 50.0], [100.0, 50.0]],
            index=dates,
            columns=columns,
        ),
        "adj_close": pd.DataFrame(
            [[90.0, 45.0], [91.0, 45.0], [91.0, 46.0]],
            index=dates,
            columns=columns,
        ),
        "volume": pd.DataFrame(1_000.0, index=dates, columns=columns),
    }
    semantics = PriceSemantics.from_wide(wide)
    version = SimpleNamespace(
        version_id="watch-v1",
        run_id="watch-run",
        target_session=pd.Timestamp("2024-01-04").date(),
        checksum_sha256="bars-sha",
        manifest_checksum_sha256="manifest-sha",
    )
    benchmark, contract = _resolve_bundle_benchmark(
        requested_universe="WATCHLIST_TEST",
        data_universe="WATCHLIST_TEST",
        version=version,
        prices=semantics,
        start=dates.min(),
        end=dates.max(),
        reader=SimpleNamespace(),
    )
    expected = semantics.total_return_open.pct_change(fill_method=None).shift(-1).mean(axis=1)
    pd.testing.assert_series_equal(benchmark, expected.rename("Benchmark"))
    assert contract["ticker"] is None
    assert contract["source"] == "UNREGISTERED_EQUAL_WEIGHT_TOTAL_RETURN_BASKET"
