"""Event-level historical backtest for the momentum-breakout / cup-handle scanner.

The live scanner describes setups; this module tests whether those setup events
had forward economic value.  It freezes each signal using data available at the
decision-date close, enters at the next executable open, attributes performance
with dividend-adjusted total-return prices, and records censored horizons rather
than dropping them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from src.breakouts.daily_data import load_breakout_daily_dataset
from src.breakouts.scanner import BreakoutFilters, evaluate_daily_setup
from src.data.price_semantics import PriceSemantics
from src.data.universe_ids import US_LIQUID_5M, resolve_market_data_universe


DEFAULT_HORIZONS = (1, 5, 20)


@dataclass(frozen=True)
class BreakoutBacktestConfig:
    trigger_statuses: tuple[str, ...] = ("BREAKOUT",)
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    cooldown_sessions: int = 20
    warmup_sessions: int = 80
    round_trip_cost_bps: float = 20.0
    require_base_pass: bool = True

    def normalized(self) -> "BreakoutBacktestConfig":
        statuses = tuple(
            dict.fromkeys(str(value).strip().upper() for value in self.trigger_statuses)
        )
        horizons = tuple(sorted({int(value) for value in self.horizons if int(value) > 0}))
        if not statuses:
            raise ValueError("trigger_statuses cannot be empty")
        if not horizons:
            raise ValueError("horizons must contain at least one positive session count")
        return BreakoutBacktestConfig(
            trigger_statuses=statuses,
            horizons=horizons,
            cooldown_sessions=max(0, int(self.cooldown_sessions)),
            warmup_sessions=max(65, int(self.warmup_sessions)),
            round_trip_cost_bps=max(0.0, float(self.round_trip_cost_bps)),
            require_base_pass=bool(self.require_base_pass),
        )


@dataclass
class BreakoutBacktestResult:
    events: pd.DataFrame
    summary: dict[str, Any]
    by_year: pd.DataFrame
    by_regime: pd.DataFrame
    config: dict[str, Any]
    data_contract: dict[str, Any] | None = None


def _frame_semantics(frame: pd.DataFrame) -> PriceSemantics:
    required = {"open", "high", "low", "close", "adj_close", "volume"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"Breakout historical frame is missing columns: {missing}")
    index = pd.DatetimeIndex(frame.index).normalize()
    wide = {
        key: pd.DataFrame({"_": pd.to_numeric(frame[key], errors="coerce")}, index=index)
        for key in ("open", "close", "adj_close", "volume")
    }
    return PriceSemantics.from_wide(wide)


def _adjusted_intraday_prices(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    close = pd.to_numeric(frame["close"], errors="coerce")
    adj = pd.to_numeric(frame["adj_close"], errors="coerce")
    factor = (adj / close).where((close > 0) & (adj > 0))
    low = pd.to_numeric(frame["low"], errors="coerce") * factor
    high = pd.to_numeric(frame["high"], errors="coerce") * factor
    return low, high


def _net_return(gross_return: float, cost_bps: float) -> float:
    if not np.isfinite(gross_return):
        return np.nan
    # Cost is represented as a deterministic round-trip return drag. Detailed
    # live fee/slippage models can later be injected without changing event time.
    return float(gross_return - cost_bps / 10000.0)


def _event_outcomes(
    frame: pd.DataFrame,
    *,
    signal_position: int,
    horizons: Sequence[int],
    round_trip_cost_bps: float,
) -> dict[str, Any]:
    dates = pd.DatetimeIndex(frame.index).normalize()
    execution_position = signal_position + 1
    if execution_position >= len(frame):
        return {
            "execution_date": None,
            "entry_open": np.nan,
            "entry_total_return_open": np.nan,
            "censored_entry": True,
        }

    semantics = _frame_semantics(frame)
    execution_open = semantics.execution_open["_"]
    total_return_open = semantics.total_return_open["_"]
    low_adjusted, high_adjusted = _adjusted_intraday_prices(frame)
    entry_date = dates[execution_position]
    entry_open = float(execution_open.iloc[execution_position])
    entry_total = float(total_return_open.iloc[execution_position])
    out: dict[str, Any] = {
        "execution_date": entry_date.strftime("%Y-%m-%d"),
        "entry_open": entry_open,
        "entry_total_return_open": entry_total,
        "censored_entry": False,
    }

    for horizon in horizons:
        exit_position = execution_position + int(horizon)
        prefix = f"h{int(horizon)}"
        if exit_position >= len(frame):
            out[f"{prefix}_exit_date"] = None
            out[f"{prefix}_gross_return"] = np.nan
            out[f"{prefix}_net_return"] = np.nan
            out[f"{prefix}_mae"] = np.nan
            out[f"{prefix}_mfe"] = np.nan
            out[f"{prefix}_censored"] = True
            continue
        exit_total = float(total_return_open.iloc[exit_position])
        gross = exit_total / entry_total - 1.0
        path_low = low_adjusted.iloc[execution_position : exit_position + 1]
        path_high = high_adjusted.iloc[execution_position : exit_position + 1]
        mae = float(path_low.min() / entry_total - 1.0) if path_low.notna().any() else np.nan
        mfe = float(path_high.max() / entry_total - 1.0) if path_high.notna().any() else np.nan
        out[f"{prefix}_exit_date"] = dates[exit_position].strftime("%Y-%m-%d")
        out[f"{prefix}_gross_return"] = gross
        out[f"{prefix}_net_return"] = _net_return(gross, round_trip_cost_bps)
        out[f"{prefix}_mae"] = mae
        out[f"{prefix}_mfe"] = mfe
        out[f"{prefix}_censored"] = False
    return out


def generate_breakout_events(
    frames: Mapping[str, pd.DataFrame],
    *,
    filters: BreakoutFilters | None = None,
    config: BreakoutBacktestConfig | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    names: Mapping[str, str] | None = None,
    sectors: Mapping[str, str] | None = None,
    market_regime: pd.Series | None = None,
) -> pd.DataFrame:
    """Freeze non-overlapping historical signal events from daily scanner logic."""
    cfg = (config or BreakoutBacktestConfig()).normalized()
    filters = (filters or BreakoutFilters()).normalized()
    start_ts = pd.Timestamp(start).normalize() if start is not None else None
    end_ts = pd.Timestamp(end).normalize() if end is not None else None
    rows: list[dict[str, Any]] = []

    for raw_ticker, raw_frame in frames.items():
        ticker = str(raw_ticker).strip().upper()
        if raw_frame is None or raw_frame.empty:
            continue
        frame = raw_frame.copy()
        frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
        frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
        if "adj_close" not in frame.columns:
            frame["adj_close"] = frame["close"]
        if len(frame) <= cfg.warmup_sessions:
            continue

        previous_status: str | None = None
        last_event_position = -10**9
        for position in range(cfg.warmup_sessions - 1, len(frame)):
            dt = pd.Timestamp(frame.index[position]).normalize()
            if start_ts is not None and dt < start_ts:
                continue
            if end_ts is not None and dt > end_ts:
                break
            setup = evaluate_daily_setup(
                frame.iloc[: position + 1],
                ticker=ticker,
                filters=filters,
                asof=dt,
                name=str((names or {}).get(ticker) or ""),
                sector=str((sectors or {}).get(ticker) or ""),
            )
            if setup is None:
                continue
            status = str(setup.get("status") or "").upper()
            transitioned = status in cfg.trigger_statuses and previous_status != status
            cooled_down = position - last_event_position > cfg.cooldown_sessions
            base_ok = bool(setup.get("base_pass")) or not cfg.require_base_pass
            if transitioned and cooled_down and base_ok:
                outcomes = _event_outcomes(
                    frame,
                    signal_position=position,
                    horizons=cfg.horizons,
                    round_trip_cost_bps=cfg.round_trip_cost_bps,
                )
                regime = None
                if market_regime is not None:
                    try:
                        regime = market_regime.reindex([dt]).iloc[0]
                    except Exception:
                        regime = None
                event_id = f"{ticker}:{dt.strftime('%Y%m%d')}:{status}"
                rows.append(
                    {
                        "event_id": event_id,
                        "ticker": ticker,
                        "signal_date": dt.strftime("%Y-%m-%d"),
                        "trigger_status": status,
                        "score": float(setup.get("score") or 0.0),
                        "pivot": float(setup.get("pivot") or np.nan),
                        "signal_close": float(setup.get("close") or np.nan),
                        "return_20d": float(setup.get("return_20d") or np.nan),
                        "adr_20d": float(setup.get("adr_20d") or np.nan),
                        "prior_move": float(setup.get("prior_move") or np.nan),
                        "consolidation_days": int(setup.get("consolidation_days") or 0),
                        "tightness": float(setup.get("tightness") or np.nan),
                        "volume_dryup": float(setup.get("volume_dryup") or np.nan),
                        "market_regime": None if pd.isna(regime) else str(regime),
                        **outcomes,
                    }
                )
                last_event_position = position
            previous_status = status

    if not rows:
        return pd.DataFrame()
    events = pd.DataFrame(rows)
    return events.sort_values(["signal_date", "ticker"]).reset_index(drop=True)


def _summary_for_events(
    events: pd.DataFrame,
    horizons: Sequence[int],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "events": int(len(events)),
        "entry_censored": int(events.get("censored_entry", pd.Series(dtype=bool)).fillna(False).sum()),
    }
    for horizon in horizons:
        prefix = f"h{int(horizon)}"
        returns = pd.to_numeric(events.get(f"{prefix}_net_return"), errors="coerce").dropna()
        censored = events.get(f"{prefix}_censored", pd.Series(dtype=bool)).fillna(True)
        mae = pd.to_numeric(events.get(f"{prefix}_mae"), errors="coerce").dropna()
        mfe = pd.to_numeric(events.get(f"{prefix}_mfe"), errors="coerce").dropna()
        out[f"{prefix}_observations"] = int(len(returns))
        out[f"{prefix}_censored"] = int(censored.sum())
        out[f"{prefix}_mean_return"] = float(returns.mean()) if len(returns) else np.nan
        out[f"{prefix}_median_return"] = float(returns.median()) if len(returns) else np.nan
        out[f"{prefix}_win_rate"] = float((returns > 0).mean()) if len(returns) else np.nan
        out[f"{prefix}_false_breakout_rate"] = float((returns <= 0).mean()) if len(returns) else np.nan
        out[f"{prefix}_mean_mae"] = float(mae.mean()) if len(mae) else np.nan
        out[f"{prefix}_mean_mfe"] = float(mfe.mean()) if len(mfe) else np.nan
    return out


def _group_summary(
    events: pd.DataFrame,
    group: str,
    horizons: Sequence[int],
) -> pd.DataFrame:
    if events.empty or group not in events.columns:
        return pd.DataFrame()
    rows = []
    for value, subset in events.dropna(subset=[group]).groupby(group, sort=True):
        rows.append({group: value, **_summary_for_events(subset, horizons)})
    return pd.DataFrame(rows).set_index(group) if rows else pd.DataFrame()


def backtest_breakout_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    filters: BreakoutFilters | None = None,
    config: BreakoutBacktestConfig | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    names: Mapping[str, str] | None = None,
    sectors: Mapping[str, str] | None = None,
    market_regime: pd.Series | None = None,
    data_contract: dict[str, Any] | None = None,
) -> BreakoutBacktestResult:
    cfg = (config or BreakoutBacktestConfig()).normalized()
    events = generate_breakout_events(
        frames,
        filters=filters,
        config=cfg,
        start=start,
        end=end,
        names=names,
        sectors=sectors,
        market_regime=market_regime,
    )
    if not events.empty:
        events["signal_year"] = pd.to_datetime(events["signal_date"]).dt.year
    return BreakoutBacktestResult(
        events=events,
        summary=_summary_for_events(events, cfg.horizons) if not events.empty else {"events": 0},
        by_year=_group_summary(events, "signal_year", cfg.horizons),
        by_regime=_group_summary(events, "market_regime", cfg.horizons),
        config=asdict(cfg),
        data_contract=data_contract,
    )


def backtest_breakouts(
    tickers: Iterable[str],
    *,
    start: str,
    end: str | None = None,
    data_universe: str = US_LIQUID_5M,
    dataset_version_id: str | None = None,
    filters: BreakoutFilters | None = None,
    config: BreakoutBacktestConfig | None = None,
    market_regime: pd.Series | None = None,
) -> BreakoutBacktestResult:
    """Run a version-bound historical event study from published daily bars."""
    cfg = (config or BreakoutBacktestConfig()).normalized()
    normalized = list(
        dict.fromkeys(str(value).strip().upper() for value in tickers if str(value).strip())
    )
    if not normalized:
        raise ValueError("tickers cannot be empty")
    # Calendar buffer is only a loader warm-up. Signals before `start` are never
    # emitted, so the buffer cannot leak future information into event creation.
    load_start = (
        pd.Timestamp(start).normalize()
        - pd.Timedelta(days=max(180, cfg.warmup_sessions * 3))
    )
    dataset = load_breakout_daily_dataset(
        requested_universe=data_universe,
        data_universe=resolve_market_data_universe(data_universe),
        tickers=normalized,
        start=load_start,
        end=end,
        dataset_version_id=dataset_version_id,
        lookback_calendar_days=max(400, cfg.warmup_sessions * 4),
    )
    universe = dataset.universe.copy()
    names = (
        universe.drop_duplicates("ticker").set_index("ticker")["name"].fillna("").astype(str).to_dict()
        if "name" in universe.columns
        else {}
    )
    sectors = (
        universe.drop_duplicates("ticker").set_index("ticker")["sector"].fillna("").astype(str).to_dict()
        if "sector" in universe.columns
        else {}
    )
    return backtest_breakout_frames(
        dataset.frames,
        filters=filters,
        config=cfg,
        start=start,
        end=end,
        names=names,
        sectors=sectors,
        market_regime=market_regime,
        data_contract=dataset.contract.to_dict(),
    )


__all__ = [
    "BreakoutBacktestConfig",
    "BreakoutBacktestResult",
    "backtest_breakout_frames",
    "backtest_breakouts",
    "generate_breakout_events",
]
