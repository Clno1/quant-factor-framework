from __future__ import annotations

import pandas as pd
import pytest

import src.data.fmp as fmp
import src.market_regime_research.sources as sources
from src.market_regime_research.models import DataContractError
from src.market_regime_research.sources import (
    combine_price_semantics,
    load_prepared_credit,
    load_prepared_prices,
    parse_cboe_history,
    parse_fred_series,
    prepare_market_sources,
    price_path,
)
from src.market_regime_research.settings import (
    MarketRegimeResearchSettings,
    PriceInstrumentSettings,
)


def _ohlcv(index: pd.DatetimeIndex, *, scale: float = 1.0) -> pd.DataFrame:
    close = pd.Series(range(100, 100 + len(index)), index=index, dtype=float) * scale
    return pd.DataFrame(
        {
            "open": close - 0.5 * scale,
            "high": close + 1.0 * scale,
            "low": close - 1.0 * scale,
            "close": close,
            "adj_close": close,
            "volume": 1_000.0,
        },
        index=index,
    )


def _volatility(index: pd.DatetimeIndex) -> pd.DataFrame:
    values: dict[str, object] = {}
    for offset, symbol in enumerate(sources.CBOE_INDEX_URLS):
        close = pd.Series(
            16.0 + offset + 0.25 * pd.RangeIndex(len(index)),
            index=index,
        )
        values[f"{symbol}_open"] = close - 0.25
        values[f"{symbol}_high"] = close + 0.5
        values[f"{symbol}_low"] = close - 0.5
        values[symbol] = close
        values[f"{symbol}_available_at"] = sources._availability_at(
            index,
            hour=17,
        )
    return pd.DataFrame(values, index=index)


def test_fmp_complete_history_splits_long_ranges(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_fetch(symbol, start, end, *, dividend_adjusted):
        calls.append((start, end))
        index = pd.date_range(start, end, freq="30D")
        return _ohlcv(index)

    monkeypatch.setattr(fmp, "get_historical_ohlcv", fake_fetch)
    result = fmp.get_historical_ohlcv_complete(
        "SPY",
        "1990-01-01",
        "2015-01-01",
        chunk_years=10,
    )

    assert calls == [
        ("1990-01-01", "1999-12-31"),
        ("2000-01-01", "2009-12-31"),
        ("2010-01-01", "2015-01-01"),
    ]
    assert result.index.is_monotonic_increasing
    assert not result.index.has_duplicates


def test_fmp_complete_history_rejects_a_possible_5000_row_truncation(monkeypatch):
    index = pd.date_range("2000-01-01", periods=5_000, freq="D")
    monkeypatch.setattr(
        fmp,
        "get_historical_ohlcv",
        lambda *args, **kwargs: _ohlcv(index),
    )
    with pytest.raises(RuntimeError, match="may be truncated"):
        fmp.get_historical_ohlcv_complete(
            "SPY",
            "2000-01-01",
            "2001-01-01",
        )


def test_price_contract_preserves_market_and_total_return_ohlc():
    index = pd.date_range("2026-01-02", periods=3, freq="B")
    market = _ohlcv(index)
    adjusted = _ohlcv(index, scale=0.8)

    result = combine_price_semantics(market, adjusted, symbol="SPY")

    assert result["close"].tolist() == market["close"].tolist()
    assert result["adj_close"].tolist() == adjusted["close"].tolist()
    assert str(result["available_at"].dtype) == "datetime64[us, UTC]"


def test_price_contract_rejects_different_adjusted_calendar():
    market = _ohlcv(pd.date_range("2026-01-02", periods=3, freq="B"))
    adjusted = market.iloc[:-1]
    with pytest.raises(DataContractError, match="calendars differ"):
        combine_price_semantics(market, adjusted, symbol="SPY")


def test_parse_official_cboe_history():
    payload = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "01/02/2026,15,17,14,16\n"
        "01/05/2026,16,18,15,17\n"
    )
    result = parse_cboe_history(payload, symbol="VIX")

    assert list(result.columns) == [
        "VIX_open",
        "VIX_high",
        "VIX_low",
        "VIX",
        "VIX_available_at",
    ]
    assert result.loc[pd.Timestamp("2026-01-05"), "VIX"] == 17


def test_parse_official_cboe_correlation_allows_valid_negative_levels():
    payload = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "01/02/2026,-1,1,-2,-0.5\n"
    )

    result = parse_cboe_history(payload, symbol="COR1M")

    assert result.loc[pd.Timestamp("2026-01-02"), "COR1M"] == -0.5


def test_parse_cboe_correlation_retains_official_intraday_outlier():
    payload = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "04/07/2025,50.29,526.02,24.44,45.22\n"
    )

    result = parse_cboe_history(payload, symbol="COR1M")

    assert result.loc[pd.Timestamp("2025-04-07"), "COR1M"] == 45.22
    assert sources._cboe_domain_anomalies(result, "COR1M") == 1


def test_parse_cboe_correlation_rejects_out_of_domain_close():
    payload = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "01/02/2026,40,101,39,101\n"
    )

    with pytest.raises(DataContractError, match="close values outside"):
        parse_cboe_history(payload, symbol="COR1M")


def test_cboe_bundle_records_correlation_ohlc_domain_anomalies():
    regular = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "01/02/2026,15,17,14,16\n"
    ).encode()
    correlation = (
        "DATE,OPEN,HIGH,LOW,CLOSE\n"
        "01/02/2026,40,526.02,39,41\n"
    ).encode()

    def downloader(url):
        return correlation if "COR1M" in url else regular

    _, metadata = sources.fetch_cboe_volatility_history(downloader=downloader)
    cor1m = next(
        item for item in metadata["sources"] if item["instrument"] == "COR1M"
    )

    assert cor1m["ohlc_domain_anomalies"] == 1


def test_fred_series_uses_conservative_next_business_day_availability():
    payload = (
        "observation_date,BAMLH0A0HYM2\n"
        "2026-01-02,3.25\n"
        "2026-01-03,.\n"
        "2026-01-05,3.30\n"
    )
    result = parse_fred_series(
        payload,
        series_id="BAMLH0A0HYM2",
        output_name="hy_oas",
    )

    available = result.loc[
        pd.Timestamp("2026-01-02"),
        "hy_oas_available_at",
    ]
    assert available.tz_convert("America/New_York").date().isoformat() == "2026-01-05"
    assert pd.Timestamp("2026-01-03") not in result.index


def test_fred_three_year_history_is_rejected_as_truncated():
    index = pd.date_range("2023-08-21", periods=786, freq="B")
    frame = pd.DataFrame({"hy_oas": 3.0}, index=index)

    with pytest.raises(DataContractError, match="only three years"):
        sources._require_complete_hy_oas_history(frame)


def test_full_hy_oas_history_gate_accepts_historical_cache():
    index = pd.bdate_range(
        sources.HY_OAS_EXPECTED_START,
        periods=sources.HY_OAS_MIN_OBSERVATIONS,
    )
    frame = pd.DataFrame({"hy_oas": 3.0}, index=index)

    sources._require_complete_hy_oas_history(frame)


def test_prepared_loader_rejects_an_incomplete_future_daily_bar(tmp_path):
    index = pd.date_range("2099-01-02", periods=3, freq="B")
    contracted = combine_price_semantics(
        _ohlcv(index),
        _ohlcv(index),
        symbol="SPY",
    )
    settings = MarketRegimeResearchSettings(
        primary_symbol="SPY",
        raw_root=tmp_path,
        instruments=(PriceInstrumentSettings("SPY", "2099-01-02", "etf"),),
    )
    path = price_path(settings, "SPY")
    path.parent.mkdir(parents=True)
    contracted.to_parquet(path)

    with pytest.raises(DataContractError, match="not yet available"):
        load_prepared_prices(settings)


def test_prepare_trims_cached_prices_and_cboe_to_latest_completed_session(
    tmp_path,
    monkeypatch,
):
    index = pd.to_datetime(["2026-01-02", "2026-01-05", "2026-01-06"])
    contracted = combine_price_semantics(
        _ohlcv(index),
        _ohlcv(index),
        symbol="SPY",
    )
    settings = MarketRegimeResearchSettings(
        primary_symbol="SPY",
        end="2026-01-10",
        raw_root=tmp_path,
        instruments=(PriceInstrumentSettings("SPY", "2026-01-02", "etf"),),
    )
    path = price_path(settings, "SPY")
    path.parent.mkdir(parents=True)
    contracted.to_parquet(path)
    volatility = _volatility(index)
    volatility.to_parquet(settings.volatility_path)
    monkeypatch.setattr(
        sources,
        "latest_completed_xnys_session",
        lambda **kwargs: pd.Timestamp("2026-01-05"),
    )

    prepare_market_sources(settings, include_credit=False)

    assert pd.read_parquet(path).index.max() == pd.Timestamp("2026-01-05")
    assert (
        pd.read_parquet(settings.volatility_path).index.max()
        == pd.Timestamp("2026-01-05")
    )


def test_prepared_loader_rejects_a_missing_internal_xnys_session(
    tmp_path,
    monkeypatch,
):
    index = pd.to_datetime(["2026-01-02", "2026-01-06"])
    contracted = combine_price_semantics(
        _ohlcv(index),
        _ohlcv(index),
        symbol="SPY",
    )
    settings = MarketRegimeResearchSettings(
        primary_symbol="SPY",
        end="2026-01-06",
        raw_root=tmp_path,
        instruments=(PriceInstrumentSettings("SPY", "2026-01-02", "etf"),),
    )
    path = price_path(settings, "SPY")
    path.parent.mkdir(parents=True)
    contracted.to_parquet(path)
    monkeypatch.setattr(
        sources,
        "latest_completed_xnys_session",
        lambda **kwargs: pd.Timestamp("2026-01-06"),
    )

    with pytest.raises(DataContractError, match="missing_sessions=1"):
        load_prepared_prices(settings)


def test_cached_cboe_rejects_availability_before_1700():
    index = pd.DatetimeIndex(["2026-01-02"])
    frame = _volatility(index)
    frame["VIX_available_at"] = sources._availability_at(index, hour=16)

    with pytest.raises(DataContractError, match="predates the 17:00"):
        sources._validate_volatility_contract(frame)


def test_cached_credit_rejects_availability_before_conservative_release():
    index = pd.DatetimeIndex(["2026-01-02"])
    frame = pd.DataFrame(
        {
            "hy_oas": [3.25],
            "hy_oas_available_at": sources._availability_at(
                pd.DatetimeIndex(["2026-01-02"]),
                hour=18,
            ),
        },
        index=index,
    )

    with pytest.raises(DataContractError, match="predates"):
        sources._validate_credit_contract(frame)


def test_prepared_credit_rejects_future_available_rows(tmp_path):
    index = pd.DatetimeIndex(["2099-01-02"])
    settings = MarketRegimeResearchSettings(raw_root=tmp_path)
    frame = pd.DataFrame(
        {
            "hy_oas": [3.25],
            "hy_oas_available_at": sources._availability_at(
                index + pd.offsets.BusinessDay(1),
                hour=18,
            ),
        },
        index=index,
    )
    frame.to_parquet(settings.credit_path)

    with pytest.raises(DataContractError, match="not yet available"):
        load_prepared_credit(settings)
