from __future__ import annotations

from datetime import date

import pandas as pd

from scripts.run_data_pipeline import _research_initial_start


def test_registered_research_universes_use_fixed_history_baseline():
    assert _research_initial_start("SP500") == "2020-01-01"
    assert _research_initial_start("NASDAQ100") == "2020-01-01"
    assert _research_initial_start("MAG7") == "2020-01-01"


def test_non_research_universe_keeps_writer_default_history_policy():
    assert _research_initial_start("CUSTOM_WATCHLIST") is None


def test_factor_research_binds_the_requested_target_session(monkeypatch):
    import scripts.run_mvp as run_mvp

    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(run_mvp, "_enabled_universes", lambda: ["MAG7"])
    monkeypatch.setattr(
        run_mvp,
        "run_pipeline_for_universe",
        lambda universe, **kwargs: calls.append((kwargs["start"], kwargs["end"])),
    )
    monkeypatch.setattr(
        "src.research_universes.service.publish_cross_universe_assessments",
        lambda **kwargs: {
            "generation_id": "test-generation",
            "target_session": str(kwargs.get("target_session") or ""),
            "verdict_counts": {},
        },
    )

    failures = run_mvp.run_pipeline(
        only_universe="MAG7",
        target_session=pd.Timestamp(date(2026, 8, 7)),
    )

    assert failures == []
    assert len(calls) == 1
    assert calls[0][1] == "2026-08-07"


def test_factor_warmup_rows_are_excluded_from_the_research_window():
    from scripts.run_mvp import _research_index

    dates = pd.date_range("2020-01-02", "2021-01-08", freq="B")

    selected = _research_index(
        dates,
        start="2021-01-04",
        end="2021-01-07",
    )

    assert selected.tolist() == list(
        pd.to_datetime(
            ["2021-01-04", "2021-01-05", "2021-01-06", "2021-01-07"]
        )
    )
