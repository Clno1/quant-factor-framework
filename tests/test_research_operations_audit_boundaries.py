from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import permutations
from unittest.mock import Mock

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd
import pytest

from src.market_regime_research import sources
from src.market_regime_research.features import compute_breadth_features
from src.market_regime_research.models import DataContractError, FeatureDefinition
from src.market_regime_research.pipeline import _validate_full_pit_feature_coverage
from src.market_regime_research.screening import _average_precision, validation_windows, run_univariate_screening
from src.market_regime_research.settings import MarketRegimeResearchSettings, PriceInstrumentSettings, _validate
from src.operations.models import JobSnapshot, JobStatus
from src.operations.registry import OperationsRegistry
from src.operations.store import OperationsReader, OperationsStore
from src.operations_web.app import create_app


def test_average_precision_threshold_ties_are_order_invariant():
    for y in set(permutations([1., 1., 0., 0.])):
        assert _average_precision(np.array(y), np.full(4, .5)) == .5
    # At threshold .9: precision 1/2, recall 1/2; at .2: 2/3, recall 1.
    assert _average_precision(np.array([1, 0, 1, 0]), np.array([.9, .9, .2, .1])) == pytest.approx(7/12)


def test_label_embargo_validation_and_actual_sealed_boundary():
    settings = MarketRegimeResearchSettings()
    with pytest.raises(ValueError, match="maximum label horizon"):
        _validate(replace(settings, screening=replace(settings.screening, embargo_sessions=1)))
    with pytest.raises(ValueError, match="maximum label horizon"):
        _validate(replace(settings, labels=replace(settings.labels, horizons=(5, 20, 90))))
    _validate(settings)
    sessions = sources._xnys_sessions("1990-01-01", "2023-01-01")
    _, end = validation_windows(sessions, settings.screening)
    assert sessions[sessions.get_loc(end) + max(settings.labels.horizons)] < pd.Timestamp(settings.screening.holdout_start)


def test_direct_screening_cannot_bypass_embargo_validation():
    from tests.test_market_regime_screening import _screening_fixture
    features, labels, registry, candidate, settings = _screening_fixture()
    with pytest.raises(DataContractError, match="every candidate label horizon"):
        run_univariate_screening(features=features, labels=labels, feature_registry=registry,
                                 candidates=candidate, settings=replace(settings, embargo_sessions=1))


def test_pit_feature_gate_uses_declared_inception_and_still_rejects_real_gaps():
    settings = MarketRegimeResearchSettings()
    index = sources._xnys_sessions("1990-01-01", "2026-09-04")
    t = np.arange(len(index))
    prices = pd.DataFrame({"A": 100*np.exp(.0001*t + .01*np.sin(t)),
                           "B": 100*np.exp(.0001*t + .01*np.cos(t))}, index=index)
    spy = pd.Series(100*np.exp(.0001*t), index=index).loc["1993-01-29":]
    config = replace(settings.features, min_cross_section_members=2, correlation_min_members=2)
    bundle = compute_breadth_features(prices, prices.notna(), benchmark_close=spy, settings=config)
    bundle.values["synthetic_positioning"] = 1.
    bundle.registry.append(FeatureDefinition("synthetic_positioning", "positioning_stress", "SYNTHETIC", "1", 0, "Isolate benchmark inception"))
    assert bundle.values["sp500_ew_cw_spread_1d"].notna().mean() < .95
    starts = {instrument.symbol: instrument.start for instrument in settings.instruments}
    result = _validate_full_pit_feature_coverage(bundle, minimum_coverage=.95, instrument_starts=starts)
    expected = result["groups"]["cross_section"]["expected_ranges"]["sp500_ew_cw_spread_1d"]
    assert expected["start"] == "1993-01-29"
    # Missing post-inception prefix must count as a genuine gap, not redefine availability.
    bundle.values.loc["1993-01-29":"1997-01-01", "sp500_ew_cw_spread_1d"] = np.nan
    with pytest.raises(DataContractError, match="coverage is below"):
        _validate_full_pit_feature_coverage(bundle, minimum_coverage=.95, instrument_starts=starts)


@pytest.mark.parametrize("fresh_download", [True, False])
def test_default_prepare_refreshes_stale_cboe_and_validates_the_new_file(tmp_path, monkeypatch, fresh_download):
    from tests.test_market_regime_sources import _ohlcv, _volatility
    settings = MarketRegimeResearchSettings(
        primary_symbol="SPY", end="2026-01-06", raw_root=tmp_path,
        instruments=(PriceInstrumentSettings("SPY", "2026-01-02", "etf"),),
    )
    index = pd.DatetimeIndex(["2026-01-02", "2026-01-05", "2026-01-06"])
    raw = _ohlcv(index)
    prices = sources.combine_price_semantics(raw, raw, symbol="SPY")
    path = sources.price_path(settings, "SPY")
    path.parent.mkdir(parents=True)
    prices.to_parquet(path)
    stale = _volatility(index[:-1])
    stale.to_parquet(settings.volatility_path)
    new = _volatility(index) if fresh_download else stale
    fetch = Mock(return_value=(new, sources._cached_cboe_metadata(new)))
    monkeypatch.setattr(sources, "fetch_cboe_volatility_history", fetch)
    monkeypatch.setattr(sources, "latest_completed_xnys_session", lambda **_: index[-1])
    if fresh_download:
        sources.prepare_market_sources(settings, include_credit=False)
        assert pd.read_parquet(settings.volatility_path).index[-1] == index[-1]
    else:
        with pytest.raises(DataContractError, match="Cboe VIX is stale"):
            sources.prepare_market_sources(settings, include_credit=False)
        pd.testing.assert_frame_equal(pd.read_parquet(settings.volatility_path), stale)
    fetch.assert_called_once()


def test_read_side_detects_dead_collector_and_recovers_without_mutating_evidence(tmp_path):
    registry = OperationsRegistry("configs/operations.yaml")
    store = OperationsStore(tmp_path / "ops.sqlite3", tmp_path / "snapshot.sqlite3")
    observed = datetime(2026, 9, 4, 20, tzinfo=timezone.utc)
    store.initialize()
    store.sync_job_definitions(registry.list(), observed_at=observed.isoformat())
    def publish(when):
        store.upsert_snapshots([JobSnapshot(job_id=job.job_id, status=JobStatus.SUCCESS,
                                            observed_at=when.isoformat(), heartbeat_at=when.isoformat())
                                for job in registry.list()])
        store.publish_snapshot()
    publish(observed)
    now = [observed + timedelta(seconds=180)]
    reader = OperationsReader(store.snapshot_path, max_snapshot_age_seconds=180, clock=lambda: now[0])
    client = TestClient(create_app(registry=registry, reader=reader, credentials=None))
    assert client.get("/healthz").json()["status"] == "ok"
    now[0] += timedelta(seconds=1)
    before_bytes = store.snapshot_path.read_bytes()
    payload = client.get("/api/overview").json()
    watchdog = next(job for job in payload["jobs"] if job["job_id"] == "operations_watchdog")
    assert watchdog["status"] == "STALE"
    assert watchdog["reported_status"] == "SUCCESS"
    assert client.get("/healthz").json()["status"] == "degraded"
    assert client.get("/api/jobs/operations_watchdog").json()["snapshot"]["status"] == "STALE"
    assert any(item["code"] == "OPS_SNAPSHOT_STALE" for item in client.get("/api/incidents").json()["incidents"])
    assert "运维采集快照已过期" in client.get("/").text
    assert store.snapshot_path.read_bytes() == before_bytes
    publish(now[0])
    assert client.get("/healthz").json()["status"] == "ok"
    assert not reader.incidents()
