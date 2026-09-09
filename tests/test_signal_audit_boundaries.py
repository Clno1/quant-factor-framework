from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from src.breakouts import application, scan_cache
from src.breakouts.historical_backtest import (
    _event_outcomes, backtest_breakout_frames, BreakoutBacktestConfig,
)
from src.breakouts.live.rolling import RollingIntradayBars
from src.data.foundation import DataFoundationError, NoPublishedDataError
from src.group_analytics.adapters import PublishedEODMarketDataProvider, PublishedMarketDataError
from tests.test_group_analytics_market_provider import _MemoryReader

NY = ZoneInfo("America/New_York")


def test_minute_revision_rebuilds_already_computed_metrics():
    frame = pd.DataFrame({"open": [100.], "high": [105.], "low": [99.],
                          "close": [105.], "volume": [100.]},
                         index=pd.to_datetime(["2026-07-28 10:00"]))
    rolling = RollingIntradayBars("TEST")
    rolling.merge(frame)
    params = dict(now=datetime(2026, 7, 28, 10, 1, 8, tzinfo=NY),
                  session_date="2026-07-28", interval=1)
    assert rolling.metrics(**params)["last_price"] == 105
    final = frame.assign(high=106., low=94., close=95., volume=1000.)
    assert rolling.merge(final) == 1
    assert rolling.metrics(**params)["last_price"] == 95
    assert rolling.completed_frame(params["now"]).iloc[0].volume == 1000
    assert rolling.merge(final) == 0


@pytest.mark.parametrize("exit_open,mae,mfe", [(100., -.01, .01), (80., -.2, .01), (120., -.01, .2)])
def test_open_exit_excludes_later_extremes_but_includes_exit_gap(exit_open, mae, mfe):
    frame = pd.DataFrame({"open": [100., 100., exit_open], "high": [101., 101., 150.],
                          "low": [99., 99., 50.], "close": [100.] * 3,
                          "adj_close": [100.] * 3, "volume": [1000.] * 3},
                         index=pd.bdate_range("2026-07-27", periods=3))
    result = _event_outcomes(frame, signal_position=0, horizons=(1,), round_trip_cost_bps=0)
    assert result["h1_mae"] == pytest.approx(mae)
    assert result["h1_mfe"] == pytest.approx(mfe)


def test_all_entry_censored_events_have_a_valid_summary():
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100.,
                          "adj_close": 100., "volume": 1000.},
                         index=pd.bdate_range("2026-01-01", periods=81))
    def scanner(prefix, **_):
        return {"status": "BREAKOUT" if len(prefix) == len(frame) else "FORMING", "base_pass": True}
    with patch("src.breakouts.historical_backtest.evaluate_daily_setup", side_effect=scanner):
        result = backtest_breakout_frames({"TEST": frame}, config=BreakoutBacktestConfig(horizons=(1,)))
    assert result.summary["entry_censored"] == 1
    assert result.summary["h1_observations"] == 0
    assert result.summary["h1_censored"] == 1


@pytest.mark.parametrize("section", [slice(0, 6), slice(6, 12), slice(12, 13)])
def test_zero_cup_volume_rejected_and_cycle_remains_persistable(tmp_path, section):
    from tests.test_cup_handle import _candidate, _handle_bars, _quote
    from src.breakouts.live.cup_handle import (
        CupHandleDetector, CUP_HANDLE_ALGORITHM_VERSION, CUP_HANDLE_PARAMETER_VERSION,
    )
    from src.breakouts.live.settings import IntradayMonitorSettings
    from src.breakouts.live.state import IntradayMonitorState
    settings = IntradayMonitorSettings()
    candidate = _candidate(settings)
    bars = _handle_bars(candidate)
    for bar in bars[section]:
        bar["volume"] = 0.
    now = datetime(2026, 4, 9, 10, 35, 1, tzinfo=NY)
    evaluation = CupHandleDetector(settings).evaluate(
        candidate, _quote(bars), {"bars": bars, "error": None},
        now=now, session_date="2026-04-09", market_open=True,
    )
    assert evaluation.outcome == "REJECTED"
    json.dumps(evaluation.to_dict(), allow_nan=False)
    state = IntradayMonitorState(tmp_path / "state.sqlite3")
    state.record_cup_handle_cycle(
        {"session_date": "2026-04-09", "observed_at": now.isoformat(),
         "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION,
         "parameter_version": CUP_HANDLE_PARAMETER_VERSION}, [evaluation.to_dict()],
    )


def test_each_live_quote_keeps_its_source_time():
    from src.alerts.engine import run_live_alert_scan
    from src.alerts.config import AlertSettings
    dates = pd.bdate_range(end="2026-07-27", periods=80)
    close = 20. * 1.02 ** np.arange(80)
    frame = pd.DataFrame({"open": close*.99, "high": close*1.04, "low": close*.96,
                          "close": close, "volume": 2e6}, index=dates)
    tickers = ["OLD_DAY", "OLD_MINUTE", "MISSING", "FRESH"]
    universe = pd.DataFrame({"ticker": tickers, "name": tickers, "sector": "Technology",
                             "asset_type": "STOCK", "current_dollar_volume": 50e6})
    stamps = [datetime(2026, 7, 27, 15, 59, tzinfo=NY).timestamp(),
              datetime(2026, 7, 28, 9, 30, tzinfo=NY).timestamp(), np.nan,
              datetime(2026, 7, 28, 10, 30, tzinfo=NY).timestamp()]
    quotes = pd.DataFrame({"price": close[-1], "open": close[-1]*.99,
                           "dayHigh": close[-1]*1.04, "dayLow": close[-1]*.96,
                           "volume": 2e6, "timestamp": stamps}, index=tickers)
    daily = SimpleNamespace(data_universe="US_LIQUID_5M", dataset_version_id="v1",
                            contract=SimpleNamespace(to_dict=lambda: {}), frame=lambda _: frame)
    with patch("src.alerts.engine._forced_tickers", return_value=set()), \
         patch("src.alerts.engine._broad_pool", return_value=(tickers, universe, {}, set(), set(), 4)), \
         patch("src.alerts.engine.get_batch_quotes", return_value=quotes), \
         patch("src.alerts.engine.load_market_regime", return_value={}):
        result = run_live_alert_scan(AlertSettings(), market_hours={"isMarketOpen": True},
                                     include_intraday=False, dataset_loader=lambda **_: daily)
    assert [row["ticker"] for row in result["rows"]] == ["FRESH"]
    assert set(result["quote_rejections"]) == set(tickers[:3])


def test_default_group_service_honors_custom_mapping(tmp_path):
    from src.group_analytics.service import GroupAnalyticsService
    from src.group_analytics.settings import GroupAnalyticsSettings, ClassificationSettings
    original = Path(__file__).parents[1] / "configs/classifications/fmp_group_ids.yaml"
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(original.read_text().replace('version: "2026-07-16"', 'version: "custom-v2"'))
    settings = GroupAnalyticsSettings(classification=ClassificationSettings(group_id_mapping_path=str(mapping)))
    service = GroupAnalyticsService(settings, market_provider=Mock(), artifact_store=Mock())
    assert service.classification_provider.group_id_mapping.version == "custom-v2"


@pytest.mark.parametrize("required,corrupt", [(False, False), (True, False), (False, True)])
def test_optional_benchmark_absence_is_distinct_from_corruption(tmp_path, required, corrupt):
    reader = _MemoryReader(tmp_path)
    original = reader.require_latest
    def require(universe, **kwargs):
        if universe != "SP500":
            raise (DataFoundationError("checksum mismatch") if corrupt else NoPublishedDataError("missing"))
        return original(universe, **kwargs)
    reader.require_latest = require
    provider = PublishedEODMarketDataProvider(reader=reader, require_benchmark=required)
    if required or corrupt:
        with pytest.raises(PublishedMarketDataError):
            provider.snapshot(symbols=["AAPL"], benchmark="SPY")
    else:
        result = provider.snapshot(symbols=["AAPL"], benchmark="SPY")
        assert result.adj_close["AAPL"].notna().all()
        assert result.benchmark_adj_close["SPY"].isna().all()
        assert provider.last_diagnostics["benchmark_dataset_version_id"] is None


@pytest.mark.parametrize("universe", ["US_ACTIVE", "watchlist:mine"])
def test_custom_scan_queue_build_cache_and_version_binding(tmp_path, monkeypatch, universe):
    monkeypatch.setattr(scan_cache, "_CACHE_DIR", tmp_path)
    context = {"data_universe": "US_LIQUID_5M", "dataset_version_id": "v1"}
    if universe.startswith("watchlist:"):
        context["watchlist_snapshot_sha256"] = "snapshot1"
    monkeypatch.setattr(application, "resolve_breakout_universe", lambda *a, **k: context)
    builder = Mock(return_value={"rows": [{"ticker": "TEST"}]})
    monkeypatch.setattr(application, "build_breakout_scan", builder)
    params = dict(universe=universe, enabled_universes=("US_ACTIVE",), asof=None,
                  min_return_20d=27., min_adr_20d=6., min_dollar_volume_m=10.,
                  min_avg_dollar_volume_m=10., min_consolidation_days=9,
                  max_distance_ma50=35., pivot_proximity=3., market_symbol="QQQ", view="ready",
                  allow_build=False)
    for _ in range(2):
        with pytest.raises(application.BreakoutScanNotReadyError):
            application.get_breakout_scan(**params)
    builder.assert_not_called()
    assert len(list((tmp_path / "requests").glob("*.json"))) == 1
    assert application.process_pending_scan_requests()[0]["status"] == "SUCCESS"
    assert builder.call_args.kwargs["dataset_version_id"] == "v1"
    assert builder.call_args.kwargs["min_return_20d"] == 27.
    assert application.get_breakout_scan(**params) == builder.return_value
    if universe.startswith("watchlist:"):
        context["watchlist_snapshot_sha256"] = "snapshot2"
        with pytest.raises(application.BreakoutScanNotReadyError):
            application.get_breakout_scan(**params)
    context["dataset_version_id"] = "v2"
    with pytest.raises(application.BreakoutScanNotReadyError):
        application.get_breakout_scan(**params)


def test_scan_worker_retries_are_bounded_and_interruption_is_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr(scan_cache, "_CACHE_DIR", tmp_path)
    scan_cache.request_scan_build({"dataset_version_id": "v1"}, enabled_universes=[])
    builder = Mock(side_effect=RuntimeError("bad data"))
    for attempt in (1, 2, 3):
        result = scan_cache.process_scan_build_requests(builder)[0]
        assert result["attempts"] == attempt
        assert result["status"] == "FAILED"
    assert scan_cache.process_scan_build_requests(builder) == []
    scan_cache.request_scan_build({"dataset_version_id": "v1"}, enabled_universes=[], force=True)
    path = next((tmp_path / "requests").glob("*.json"))
    job = json.loads(path.read_text())
    job.update(status="RUNNING", attempts=1)
    path.write_text(json.dumps(job))
    assert scan_cache.process_scan_build_requests(lambda **_: {"rows": []})[0]["status"] == "SUCCESS"


def test_queued_watchlist_build_rejects_a_changed_snapshot(monkeypatch):
    monkeypatch.setattr(application, "resolve_breakout_universe", lambda *a, **k: {
        "watchlist_snapshot_sha256": "edited",
    })
    with pytest.raises(application.BreakoutApplicationError, match="股票池已变更"):
        application.build_breakout_scan(
            universe="watchlist:mine", enabled_universes=[], asof=None,
            min_return_20d=27., min_adr_20d=6., min_dollar_volume_m=10.,
            min_avg_dollar_volume_m=10., min_consolidation_days=9,
            max_distance_ma50=35., pivot_proximity=3., market_symbol="QQQ", view="all",
            dataset_version_id="v1", watchlist_snapshot_sha256="original",
        )


def test_existing_background_entry_point_processes_queued_scans(monkeypatch):
    from scripts.run_data_requests import main
    monkeypatch.setattr("src.data.request_worker.process_pending_data_requests", lambda **_: [])
    monkeypatch.setattr("src.utils.env.load_local_env", lambda *_: None)
    worker = Mock(return_value=[])
    monkeypatch.setattr(application, "process_pending_scan_requests", worker)
    assert main([]) == 0
    worker.assert_called_once_with(limit=1)
