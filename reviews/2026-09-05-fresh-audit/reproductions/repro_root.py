"""Fresh review: offline reproductions against normal repository imports.

Run with the isolated repository as cwd and on PYTHONPATH.
Assertions confirm the observed defects, rather than desired behavior.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import numpy as np
import pandas as pd

from src.market_regime_research.settings import (
    MarketRegimeResearchSettings, PriceInstrumentSettings, _validate,
)
from src.market_regime_research.models import FeatureBundle, FeatureDefinition, DataContractError
from src.market_regime_research.pipeline import _validate_full_pit_feature_coverage
from src.market_regime_research.features import compute_breadth_features
from src.market_regime_research import sources
from src.operations.models import JobSnapshot, JobStatus
from src.operations.registry import OperationsRegistry
from src.operations.store import OperationsStore, OperationsReader


def average_precision_ties():
    from src.market_regime_research.screening import _average_precision
    score = np.full(4, 0.5)
    first = _average_precision(np.array([1., 1., 0., 0.]), score)
    last = _average_precision(np.array([0., 0., 1., 1.]), score)
    assert first == 1.0 and np.isclose(last, 5 / 12)
    return {"all_scores": score.tolist(), "positive_first": first,
            "positive_last": last, "expected_both": 0.5}


def embargo_horizon_mismatch():
    from src.market_regime_research.screening import validation_windows
    settings = MarketRegimeResearchSettings()
    settings = replace(settings, screening=replace(settings.screening, embargo_sessions=1))
    _validate(settings)
    sessions = sources._xnys_sessions("1990-01-01", "2023-01-01")
    _, end = validation_windows(sessions, settings.screening)
    label_end = sessions[sessions.get_loc(end) + max(settings.labels.horizons)]
    assert label_end >= pd.Timestamp(settings.screening.holdout_start)
    return {"settings_accepted": True, "embargo_sessions": 1,
            "label_horizon": max(settings.labels.horizons),
            "last_development_session": str(end.date()),
            "its_label_uses_prices_until": str(label_end.date()),
            "sealed_holdout_start": settings.screening.holdout_start}


def full_pit_inception_gate():
    settings = MarketRegimeResearchSettings()
    index = sources._xnys_sessions("1990-01-01", "2026-09-04")
    t = np.arange(len(index))
    # Complete prices and membership; only SPY obeys its configured inception.
    prices = pd.DataFrame({"A": 100 * np.exp(.0001*t + .01*np.sin(t)),
                           "B": 100 * np.exp(.0001*t + .01*np.cos(t))}, index=index)
    spy = pd.Series(100 * np.exp(.0001*t), index=index).loc["1993-01-29":]
    config = replace(settings.features, min_cross_section_members=2, correlation_min_members=2)
    bundle = compute_breadth_features(prices, prices.notna(), benchmark_close=spy, settings=config)
    # Supply an always-complete independent positioning group to isolate the breadth gate.
    bundle.values["synthetic_positioning"] = 1.0
    bundle.registry.append(FeatureDefinition("synthetic_positioning", "positioning_stress",
        "SYNTHETIC", "1", 0, "Isolate inception coverage; not a claim about stress calculation"))
    coverage = float(bundle.values["sp500_ew_cw_spread_1d"].notna().mean())
    try:
        _validate_full_pit_feature_coverage(bundle, minimum_coverage=.95)
    except DataContractError as exc:
        error = str(exc)
    else:
        raise AssertionError("Expected inception gate failure")
    assert coverage < .95
    return {"sessions": len(index), "spy_spread_coverage": coverage,
            "threshold": .95, "error": error}


def stale_cboe_is_not_refreshed():
    with tempfile.TemporaryDirectory() as tmp:
        settings = MarketRegimeResearchSettings(primary_symbol="SPY", end="2026-01-06",
            raw_root=Path(tmp), instruments=(PriceInstrumentSettings("SPY", "2026-01-02", "etf"),))
        index = pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"])
        close = pd.Series([100., 101., 102.], index=index)
        raw = pd.DataFrame({"open": close, "high": close+1, "low": close-1,
            "close": close, "adj_close": close, "volume": 1000.}, index=index)
        contracted = sources.combine_price_semantics(raw, raw, symbol="SPY")
        path = sources.price_path(settings, "SPY")
        path.parent.mkdir(parents=True)
        contracted.to_parquet(path)
        stale = index[:-1]
        values = {}
        for symbol in sources.CBOE_INDEX_URLS:
            values.update({f"{symbol}_open": 16., f"{symbol}_high": 17.,
                f"{symbol}_low": 15., symbol: 16.,
                f"{symbol}_available_at": sources._availability_at(stale, hour=17)})
        pd.DataFrame(values, index=stale).to_parquet(settings.volatility_path)
        with patch.object(sources, "latest_completed_xnys_session", return_value=index[-1]), \
             patch.object(sources, "fetch_cboe_volatility_history") as fetch:
            try:
                sources.prepare_market_sources(settings, include_credit=False)
            except DataContractError as exc:
                error = str(exc)
            else:
                raise AssertionError("Expected stale cache failure")
            assert fetch.call_count == 0
        return {"expected_session": str(index[-1].date()), "cached_last_session": str(stale[-1].date()),
                "refresh_calls": 0, "error": error}


def dead_watchdog_stays_green():
    from src.operations_web.app import create_app
    from fastapi.testclient import TestClient
    with tempfile.TemporaryDirectory() as tmp:
        registry = OperationsRegistry("configs/operations.yaml")
        db = OperationsStore(Path(tmp)/"ops.sqlite3", Path(tmp)/"snapshot.sqlite3")
        observed = "2000-01-03T20:00:00+00:00"
        db.initialize()
        db.sync_job_definitions(registry.list(), observed_at=observed)
        db.upsert_snapshots([JobSnapshot(job_id=job.job_id, status=JobStatus.SUCCESS,
            observed_at=observed, heartbeat_at=observed) for job in registry.list()])
        db.publish_snapshot()
        reader = OperationsReader(Path(tmp)/"snapshot.sqlite3")
        with TestClient(create_app(registry=registry, reader=reader, credentials=None)) as client:
            health = client.get("/healthz").json()
            overview = client.get("/api/overview").json()
        watchdog = next(x for x in overview["jobs"] if x["job_id"] == "operations_watchdog")
        assert health["status"] == "ok" and watchdog["status"] == "SUCCESS"
        return {"snapshot_at": overview["snapshot_at"], "health": health,
                "watchdog_status": watchdog["status"], "snapshot_incidents": len(overview["incidents"])}


if __name__ == "__main__":
    output = {}
    for case in [average_precision_ties, embargo_horizon_mismatch, full_pit_inception_gate,
                 stale_cboe_is_not_refreshed, dead_watchdog_stays_green]:
        try:
            output[case.__name__] = case()
        except Exception as exc:
            output[case.__name__] = {"harness_error": repr(exc)}
        print(json.dumps({case.__name__: output[case.__name__]}, ensure_ascii=False), flush=True)
    Path("/private/tmp/quant_fresh_audit_20260905/repro_root_result.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2))
