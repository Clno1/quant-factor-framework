from __future__ import annotations

import pandas as pd
import pytest

import src.factors.artifacts as artifacts


def _redirect_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        artifacts,
        "factor_values_path",
        lambda name, universe="SP500": (
            tmp_path / universe / name / "factor_values.parquet"
        ),
    )
    monkeypatch.setattr(
        artifacts,
        "factor_raw_values_path",
        lambda name, universe="SP500": (
            tmp_path / universe / name / "factor_raw_values.parquet"
        ),
    )


def test_factor_raw_and_clean_publish_as_one_verified_generation(
    monkeypatch,
    tmp_path,
):
    _redirect_paths(monkeypatch, tmp_path)
    index = pd.date_range("2026-01-05", periods=3, freq="B")
    raw = pd.DataFrame({"A": [1.0, 2.0, 3.0]}, index=index)
    clean = pd.DataFrame({"A": [-1.0, 0.0, 1.0]}, index=index)

    artifacts.save_factor_matrix_bundle(
        "MOM_TEST",
        raw=raw,
        clean=clean,
        universe="TEST",
        provenance={"preprocessing": {"standardize": True}},
    )
    loaded_raw, loaded_clean, manifest = (
        artifacts.load_factor_matrix_bundle("MOM_TEST", "TEST")
    )

    pd.testing.assert_frame_equal(loaded_raw, raw, check_freq=False)
    pd.testing.assert_frame_equal(loaded_clean, clean, check_freq=False)
    assert manifest["generation_id"]
    assert manifest["provenance"]["preprocessing"]["standardize"] is True
    assert set(manifest["artifact_sha256"]) == {
        "factor_raw_values.parquet",
        "factor_values.parquet",
    }


def test_factor_bundle_rejects_misaligned_raw_and_clean(
    monkeypatch,
    tmp_path,
):
    _redirect_paths(monkeypatch, tmp_path)
    raw = pd.DataFrame(
        {"A": [1.0]},
        index=pd.DatetimeIndex(["2026-01-05"]),
    )
    clean = pd.DataFrame(
        {"B": [1.0]},
        index=pd.DatetimeIndex(["2026-01-05"]),
    )

    with pytest.raises(ValueError, match="misaligned"):
        artifacts.save_factor_matrix_bundle(
            "MOM_TEST",
            raw=raw,
            clean=clean,
            universe="TEST",
        )


def test_factor_bundle_rejects_tampered_clean_matrix(
    monkeypatch,
    tmp_path,
):
    _redirect_paths(monkeypatch, tmp_path)
    index = pd.DatetimeIndex(["2026-01-05"])
    raw = pd.DataFrame({"A": [1.0]}, index=index)
    clean = pd.DataFrame({"A": [0.0]}, index=index)
    artifacts.save_factor_matrix_bundle(
        "MOM_TEST",
        raw=raw,
        clean=clean,
        universe="TEST",
    )
    clean_path = artifacts.factor_values_path("MOM_TEST", "TEST")
    pd.DataFrame({"A": [99.0]}, index=index).to_parquet(clean_path)

    with pytest.raises(ValueError, match="hash mismatch"):
        artifacts.load_factor_matrix_bundle("MOM_TEST", "TEST")
