from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from src.decision_replay.models import DecisionReplaySnapshot
from src.decision_replay.query import _cached_snapshot, get_snapshot
from src.decision_replay.store import (
    load_snapshot,
    replay_dir,
    save_snapshot,
    upsert_snapshot,
)


def _snapshot(date: str, value: float) -> DecisionReplaySnapshot:
    index = pd.DatetimeIndex([date], name="date")
    matrix = pd.DataFrame({"A": [value]}, index=index)
    return DecisionReplaySnapshot(
        manifest={
            "schema_version": 1,
            "source_kind": "paper",
            "source_id": "test",
            "created_at": f"{date}T00:00:00+00:00",
            "factor_ids": [],
        },
        daily_summary=pd.DataFrame(
            {
                "is_rebalance": [False],
                "eligible_count": [1],
                "equity": [value],
            },
            index=index,
        ),
        market={"close": matrix.copy()},
        signals={"composite": matrix.copy()},
        factors={},
        portfolio={"daily_weights": matrix * 0.0},
    )


def test_snapshot_load_rejects_hash_tampering(tmp_path):
    save_snapshot(tmp_path, _snapshot("2026-01-05", 100.0))
    path = replay_dir(tmp_path) / "market" / "close.parquet"
    tampered = pd.read_parquet(path)
    tampered.iloc[0, 0] = 999.0
    tampered.to_parquet(path)

    with pytest.raises(ValueError, match="hash mismatch"):
        load_snapshot(tmp_path)


def test_snapshot_load_rejects_unmanifested_artifacts(tmp_path):
    save_snapshot(tmp_path, _snapshot("2026-01-05", 100.0))
    unexpected = replay_dir(tmp_path) / "unexpected.parquet"
    pd.DataFrame({"x": [1]}).to_parquet(unexpected)

    with pytest.raises(ValueError, match="artifact set does not match"):
        load_snapshot(tmp_path)


def test_complete_snapshot_rewrite_removes_previous_factor_files(tmp_path):
    first = _snapshot("2026-01-05", 100.0)
    matrix = first.signals["composite"].copy()
    first.factors = {
        "F1": {"clean": matrix.copy()},
        "F2": {"clean": matrix.copy()},
    }
    first.manifest["factor_ids"] = ["F1", "F2"]
    save_snapshot(tmp_path, first)

    second = _snapshot("2026-01-05", 101.0)
    second.factors = {"F1": {"clean": matrix.copy()}}
    second.manifest["factor_ids"] = ["F1"]
    save_snapshot(tmp_path, second)

    loaded = load_snapshot(tmp_path)
    assert loaded is not None
    assert set(loaded.factors) == {"F1"}


def test_concurrent_upserts_preserve_both_dates(tmp_path):
    snapshots = [
        _snapshot("2026-01-05", 100.0),
        _snapshot("2026-01-06", 101.0),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda item: upsert_snapshot(tmp_path, item), snapshots))

    loaded = load_snapshot(tmp_path)
    assert loaded is not None
    assert list(loaded.daily_summary.index) == list(pd.DatetimeIndex([
        "2026-01-05",
        "2026-01-06",
    ]))
    assert loaded.daily_summary["equity"].tolist() == [100.0, 101.0]


def test_snapshot_cache_invalidates_when_artifact_changes(tmp_path):
    _cached_snapshot.cache_clear()
    save_snapshot(tmp_path, _snapshot("2026-01-05", 100.0))
    assert get_snapshot(tmp_path) is not None

    path = replay_dir(tmp_path) / "market" / "close.parquet"
    tampered = pd.read_parquet(path)
    tampered.iloc[0, 0] = 999.0
    tampered.to_parquet(path)

    with pytest.raises(ValueError, match="hash mismatch"):
        get_snapshot(tmp_path)
    _cached_snapshot.cache_clear()
