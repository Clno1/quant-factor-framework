from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

import src.data.pit as pit


def _membership(rows: list[tuple[str, str, bool]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["date", "ticker", "active"]).assign(
        date=lambda values: pd.to_datetime(values["date"])
    )


def test_dynamic_and_static_universe_requirements():
    assert pit.point_in_time_required("SP500", strict=True) is True
    assert pit.point_in_time_required("US_ACTIVE", strict=True) is True
    assert pit.point_in_time_required("MAG7", strict=True) is False
    assert pit.point_in_time_required("watchlist:anything", strict=True) is False


def test_required_membership_file_cannot_silently_fall_back(monkeypatch):
    monkeypatch.setattr(
        pit,
        "load_point_in_time_membership",
        lambda universe: (None, None),
    )
    with pytest.raises(FileNotFoundError, match="No point-in-time"):
        pit.build_membership_mask(
            pd.date_range("2026-01-02", periods=2, freq="B"),
            pd.Index(["A"]),
            "SP500",
            required=True,
        )


@pytest.mark.parametrize("bad_value", [None, "", "../AAPL"])
def test_membership_loader_rejects_invalid_tickers(
    tmp_path,
    monkeypatch,
    bad_value,
):
    path = tmp_path / "SP500.csv"
    pd.DataFrame({
        "date": ["2026-01-01"],
        "ticker": [bad_value],
        "active": [True],
    }).to_csv(path, index=False)
    monkeypatch.setattr(pit, "find_membership_file", lambda _universe: path)

    with pytest.raises(ValueError, match="invalid|empty"):
        pit.load_point_in_time_membership("SP500")


def test_membership_loader_preserves_canonical_active_column(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "SP500.parquet"
    pd.DataFrame(
        {
            "date": ["2026-01-02", "2026-01-02"],
            "ticker": ["AAA", "BBB"],
            "active": [True, False],
        }
    ).to_parquet(path, index=False)
    monkeypatch.setattr(pit, "find_membership_file", lambda _universe: path)

    loaded, source = pit.load_point_in_time_membership("SP500")

    assert source == path
    assert loaded is not None
    assert loaded.columns.tolist() == ["date", "ticker", "active"]
    assert loaded.set_index("ticker")["active"].to_dict() == {
        "AAA": True,
        "BBB": False,
    }


def test_membership_mask_uses_latest_complete_snapshot(monkeypatch):
    values = _membership([
        ("2026-01-01", "A", True),
        ("2026-01-01", "B", True),
        ("2026-01-06", "B", True),
        ("2026-01-06", "C", True),
    ])
    monkeypatch.setattr(
        pit,
        "load_point_in_time_membership",
        lambda universe: (values, Path("/tmp/SP500.parquet")),
    )
    dates = pd.DatetimeIndex([
        "2026-01-02",
        "2026-01-05",
        "2026-01-06",
        "2026-01-07",
    ])

    mask, diagnostics = pit.build_membership_mask(
        dates,
        pd.Index(["A", "B", "C"]),
        "SP500",
        required=True,
    )

    assert mask is not None
    assert mask.loc[pd.Timestamp("2026-01-05")].to_dict() == {
        "A": True,
        "B": True,
        "C": False,
    }
    assert mask.loc[pd.Timestamp("2026-01-06")].to_dict() == {
        "A": False,
        "B": True,
        "C": True,
    }
    assert diagnostics.applied is True


def test_snapshot_must_cover_backtest_start(monkeypatch):
    values = _membership([
        ("2026-01-06", "A", True),
    ])
    monkeypatch.setattr(
        pit,
        "load_point_in_time_membership",
        lambda universe: (values, Path("/tmp/SP500.parquet")),
    )

    with pytest.raises(ValueError, match="after backtest start"):
        pit.build_membership_mask(
            pd.DatetimeIndex(["2026-01-05", "2026-01-06"]),
            pd.Index(["A"]),
            "SP500",
            required=True,
        )


def test_historical_members_must_exist_in_data_matrix(monkeypatch):
    values = _membership([
        ("2026-01-01", "A", True),
        ("2026-01-01", "DELISTED", True),
    ])
    monkeypatch.setattr(
        pit,
        "load_point_in_time_membership",
        lambda universe: (values, Path("/tmp/SP500.parquet")),
    )

    with pytest.raises(ValueError, match="historically active tickers absent"):
        pit.build_membership_mask(
            pd.DatetimeIndex(["2026-01-05", "2026-01-06"]),
            pd.Index(["A"]),
            "SP500",
            required=True,
        )
