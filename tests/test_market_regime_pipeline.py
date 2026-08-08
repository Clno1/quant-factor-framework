from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

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
    _load_validated_pit_metadata,
    _validate_source_manifest,
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
    membership_path = tmp_path / "SP500.parquet"
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
            "membership_sha256": file_sha256(membership_path),
            "diagnostics": {
                "quality_status": "PASS",
                "inconsistency_count": 0,
            },
        },
    )

    result = _load_validated_pit_metadata(
        membership_path,
        expected_asof=pd.Timestamp("2026-01-05"),
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
        )
