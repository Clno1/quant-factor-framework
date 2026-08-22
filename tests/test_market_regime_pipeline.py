from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from src.market_regime_research.artifacts import (
    file_sha256,
    publish_research_run,
    write_strict_json,
)
from src.market_regime_research.models import (
    DataContractError,
    FeatureBundle,
    FeatureDefinition,
)
from src.market_regime_research.pipeline import (
    _align_available_bundle,
    _validate_full_pit_feature_coverage,
    _load_validated_pit_metadata,
    _validate_source_manifest,
    _wide_tables_from_daily_bars,
    build_research_dataset,
)
from src.market_regime_research.pit import membership_metadata_path
from src.market_regime_research.settings import (
    MarketRegimeResearchSettings,
    PriceInstrumentSettings,
)
from src.market_regime_research.sources import price_path


def _price_frame(index: pd.DatetimeIndex, *, drift: float) -> pd.DataFrame:
    close = pd.Series(
        100 * np.exp(np.arange(len(index)) * drift)
        * (1 + 0.01 * np.sin(np.arange(len(index)) / 7)),
        index=index,
    )
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "adj_open": close * 0.999,
            "adj_high": close * 1.01,
            "adj_low": close * 0.99,
            "adj_close": close,
            "volume": 1_000_000 + np.arange(len(index)),
        },
        index=index,
    )


def _inputs():
    index = pd.date_range("2024-01-02", periods=400, freq="B")
    prices = {
        "^GSPC": _price_frame(index, drift=0.0004),
        "SPY": _price_frame(index, drift=0.00038),
        "QQQ": _price_frame(index, drift=0.0005),
        "IWM": _price_frame(index, drift=0.0003),
        "HYG": _price_frame(index, drift=0.00015),
        "LQD": _price_frame(index, drift=0.00010),
    }
    volatility = pd.DataFrame(
        {
            "VIX": 18 + np.sin(np.arange(len(index)) / 10),
            "VIX9D": 17 + np.sin(np.arange(len(index)) / 9),
            "VIX3M": 19 + np.sin(np.arange(len(index)) / 12),
            "COR1M": 25 + 4 * np.sin(np.arange(len(index)) / 15),
        },
        index=index,
    )
    return prices, volatility


def test_core_dataset_and_artifacts_are_self_describing(tmp_path):
    prices, volatility = _inputs()
    settings = MarketRegimeResearchSettings(output_root=tmp_path)
    features, labels, diagnostics = build_research_dataset(
        settings=settings,
        prices=prices,
        volatility=volatility,
    )

    assert not features.values.empty
    assert not labels.empty
    assert set(features.values.columns) == {
        definition.feature_name for definition in features.registry
    }
    assert diagnostics["mode"] == "market_core_only"

    result = publish_research_run(
        output_root=tmp_path,
        features=features,
        labels=labels,
        input_manifest={"fixture": "synthetic"},
        diagnostics=diagnostics,
        run_id="test_run",
    )
    manifest = json.loads(result.manifest_path.read_text())
    pointer = json.loads((tmp_path / "latest.json").read_text())

    assert manifest["run_id"] == "test_run"
    assert manifest["feature_columns"] == len(features.values.columns)
    assert pointer["run_id"] == "test_run"
    assert result.feature_registry_path.exists()


def test_cross_section_inputs_cannot_be_partially_supplied():
    prices, volatility = _inputs()
    settings = MarketRegimeResearchSettings()
    adj_close = pd.DataFrame(
        {"A": np.arange(400, dtype=float) + 100},
        index=next(iter(prices.values())).index,
    )

    with pytest.raises(DataContractError, match="must be supplied together"):
        build_research_dataset(
            settings=settings,
            prices=prices,
            volatility=volatility,
            adj_close=adj_close,
        )


def test_full_pit_feature_registry_satisfies_frozen_v2_hypotheses():
    prices, volatility = _inputs()
    index = next(iter(prices.values())).index
    time = np.arange(len(index), dtype=float)
    adj_close = pd.DataFrame(
        {
            f"S{position:02d}": (
                100
                * np.exp(time * (0.0001 + position * 0.000005))
                * (1 + 0.01 * np.sin(time / (5 + position % 7)))
            )
            for position in range(35)
        },
        index=index,
    )
    membership = pd.DataFrame(True, index=index, columns=adj_close.columns)

    features, _, diagnostics = build_research_dataset(
        settings=MarketRegimeResearchSettings(),
        prices=prices,
        volatility=volatility,
        adj_close=adj_close,
        membership_mask=membership,
    )
    registry_path = (
        Path(__file__).resolve().parents[1]
        / "configs"
        / "market_regime_screening_candidates_v2.yaml"
    )
    payload = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    referenced = {item["feature_name"] for item in payload["hypotheses"]}

    assert referenced.issubset(features.values.columns)
    assert diagnostics["mode"] == "full_pit"
    assert "breadth_above_ma120_pct" in features.values.columns
    assert "cor1m_percentile_252d" in features.values.columns


def test_version_bound_daily_bars_are_pivoted_in_memory():
    bars = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-02", "2026-01-02", "2026-01-05", "2026-01-05"]
            ),
            "ticker": ["B", "A", "B", "A"],
            "adj_close": [20.0, 10.0, 21.0, 11.0],
            "high": [21.0, 11.0, 22.0, 12.0],
            "low": [19.0, 9.0, 20.0, 10.0],
            "volume": [200.0, 100.0, 210.0, 110.0],
        }
    )

    wide = _wide_tables_from_daily_bars(bars)

    assert set(wide) == {"adj_close", "high", "low", "volume"}
    assert list(wide["adj_close"].columns) == ["A", "B"]
    assert wide["adj_close"].loc[pd.Timestamp("2026-01-05"), "A"] == 11.0


def test_available_data_is_not_forward_filled_indefinitely():
    source_index = pd.DatetimeIndex(["2026-01-02"])
    target_index = pd.date_range("2026-01-02", periods=10, freq="B")
    bundle = FeatureBundle(
        values=pd.DataFrame({"credit": [3.0]}, index=source_index),
        registry=[
            FeatureDefinition(
                feature_name="credit",
                group="credit",
                instrument="fixture",
                formula="fixture",
                lookback_sessions=1,
                description="fixture",
            )
        ],
    )

    aligned = _align_available_bundle(
        bundle,
        target_index,
        max_forward_fill_rows=2,
    )

    assert aligned.values.iloc[1, 0] == 3.0
    assert pd.isna(aligned.values.iloc[-1, 0])


def test_artifact_publication_rejects_misaligned_feature_and_label_dates(tmp_path):
    prices, volatility = _inputs()
    settings = MarketRegimeResearchSettings(output_root=tmp_path)
    features, labels, diagnostics = build_research_dataset(
        settings=settings,
        prices=prices,
        volatility=volatility,
    )

    with pytest.raises(DataContractError, match="align exactly"):
        publish_research_run(
            output_root=tmp_path,
            features=features,
            labels=labels.iloc[:-1],
            input_manifest={"fixture": "synthetic"},
            diagnostics=diagnostics,
            run_id="misaligned",
        )


def test_source_manifest_hash_mismatch_fails_closed(tmp_path):
    settings = MarketRegimeResearchSettings(
        primary_symbol="SPY",
        raw_root=tmp_path / "raw",
        instruments=(PriceInstrumentSettings("SPY", "2026-01-02", "etf"),),
    )
    spy_path = price_path(settings, "SPY")
    spy_path.parent.mkdir(parents=True)
    spy_path.write_bytes(b"price-v1")
    settings.volatility_path.write_bytes(b"volatility-v1")

    manifest = {
        "schema_version": "1.0.0",
        "configured_end": "2026-01-05",
        "credit_included": False,
        "sources": [
            {
                "path": "prices/SPY.parquet",
                "file_sha256": file_sha256(spy_path),
                "quality_status": "PASS",
            },
            {
                "path": "volatility.parquet",
                "file_sha256": file_sha256(settings.volatility_path),
                "quality_status": "PASS",
            },
        ],
    }
    spy_path.write_bytes(b"price-tampered")

    with pytest.raises(DataContractError, match="hash differs"):
        _validate_source_manifest(
            settings,
            manifest,
            include_credit=False,
            expected_end=pd.Timestamp("2026-01-05"),
        )


def test_pit_publication_metadata_is_bound_to_membership_hash(tmp_path):
    membership_path = tmp_path / "SP500_MARKET_REGIME.parquet"
    pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-01-05")],
            "ticker": ["A"],
            "active": [True],
        }
    ).to_parquet(membership_path, index=False)
    metadata_path = membership_metadata_path(membership_path)
    write_strict_json(
        metadata_path,
        {
            "schema_version": "1.0.0",
            "quality_status": "PASS",
            "strict": True,
            "asof": "2026-01-05",
            "start": "2026-01-05",
            "membership_sha256": file_sha256(membership_path),
            "diagnostics": {
                "quality_status": "PASS",
                "inconsistency_count": 0,
                "scope": "market_regime",
                "strict": True,
                "start": "2026-01-05",
                "asof": "2026-01-05",
            },
            "source": {
                "scope": "market_regime",
                "publication_id": "SP500_MARKET_REGIME",
            },
        },
    )

    result = _load_validated_pit_metadata(
        membership_path,
        expected_asof=pd.Timestamp("2026-01-05"),
        expected_start=pd.Timestamp("2026-01-05"),
        expected_scope="market_regime",
        expected_publication_id="SP500_MARKET_REGIME",
    )
    assert result["quality_status"] == "PASS"

    pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-01-05")],
            "ticker": ["B"],
            "active": [True],
        }
    ).to_parquet(membership_path, index=False)
    with pytest.raises(DataContractError, match="hash differs"):
        _load_validated_pit_metadata(
            membership_path,
            expected_asof=pd.Timestamp("2026-01-05"),
            expected_start=pd.Timestamp("2026-01-05"),
            expected_scope="market_regime",
            expected_publication_id="SP500_MARKET_REGIME",
        )


def test_main_factor_pit_scope_cannot_satisfy_market_regime_contract(tmp_path):
    membership_path = tmp_path / "SP500_MARKET_REGIME.parquet"
    pd.DataFrame(
        {
            "date": [pd.Timestamp("1990-01-01")],
            "ticker": ["A"],
            "active": [True],
        }
    ).to_parquet(membership_path, index=False)
    write_strict_json(
        membership_metadata_path(membership_path),
        {
            "schema_version": "1.0.0",
            "quality_status": "PASS",
            "strict": True,
            "asof": "2026-01-05",
            "start": "1990-01-01",
            "membership_sha256": file_sha256(membership_path),
            "diagnostics": {
                "quality_status": "PASS",
                "inconsistency_count": 0,
                "scope": "main_factor",
                "strict": True,
                "start": "1990-01-01",
                "asof": "2026-01-05",
            },
            "source": {
                "scope": "main_factor",
                "publication_id": "SP500",
            },
        },
    )

    with pytest.raises(DataContractError, match="diagnostics scope"):
        _load_validated_pit_metadata(
            membership_path,
            expected_asof=pd.Timestamp("2026-01-05"),
            expected_start=pd.Timestamp("1990-01-01"),
            expected_scope="market_regime",
            expected_publication_id="SP500_MARKET_REGIME",
        )


def test_market_regime_pit_must_cover_the_configured_history_start(tmp_path):
    membership_path = tmp_path / "SP500_MARKET_REGIME.parquet"
    pd.DataFrame(
        {
            "date": [pd.Timestamp("2020-01-01")],
            "ticker": ["A"],
            "active": [True],
        }
    ).to_parquet(membership_path, index=False)
    write_strict_json(
        membership_metadata_path(membership_path),
        {
            "schema_version": "1.0.0",
            "quality_status": "PASS",
            "strict": True,
            "asof": "2026-01-05",
            "start": "2020-01-01",
            "membership_sha256": file_sha256(membership_path),
            "diagnostics": {
                "quality_status": "PASS",
                "inconsistency_count": 0,
                "scope": "market_regime",
                "strict": True,
                "start": "2020-01-01",
                "asof": "2026-01-05",
            },
            "source": {
                "scope": "market_regime",
                "publication_id": "SP500_MARKET_REGIME",
            },
        },
    )

    with pytest.raises(DataContractError, match="begins after"):
        _load_validated_pit_metadata(
            membership_path,
            expected_asof=pd.Timestamp("2026-01-05"),
            expected_start=pd.Timestamp("1990-01-01"),
            expected_scope="market_regime",
            expected_publication_id="SP500_MARKET_REGIME",
        )


def test_full_pit_feature_coverage_gate_rejects_partial_cross_section():
    index = pd.date_range("2020-01-01", periods=10, freq="B")
    values = pd.DataFrame(
        {
            "breadth_feature": [np.nan] * 9 + [1.0],
            "cross_section_feature": 1.0,
            "positioning_feature": 1.0,
        },
        index=index,
    )
    registry = [
        FeatureDefinition("breadth_feature", "breadth", "fixture", "x", 1, "x"),
        FeatureDefinition(
            "cross_section_feature", "cross_section", "fixture", "x", 1, "x"
        ),
        FeatureDefinition(
            "positioning_feature", "positioning_stress", "fixture", "x", 1, "x"
        ),
    ]

    with pytest.raises(DataContractError, match="coverage is below"):
        _validate_full_pit_feature_coverage(
            FeatureBundle(values=values, registry=registry),
            minimum_coverage=0.95,
        )
