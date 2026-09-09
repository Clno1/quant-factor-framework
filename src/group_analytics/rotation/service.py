"""CLI-only rotation pipeline. Provider reads never occur in Web requests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from ..calendar import _calendar, latest_completed_session, official_session_close
from ..artifacts import normalize_json_value
from . import SCHEMA_VERSION, SOURCE_PROFILE, PRODUCTION_PROFILE
from .context import evaluate_context, price_response
from .engine import analyze, clean_table
from .store import RotationStore, encoded
from .themes import default_themes, required_symbols


def load_frames(symbols, start, end, *, refresh=False, cache_root=None, fetcher=None):
    """Group-owned cache; existing shared OHLCV files remain read-only fallback."""
    root = Path(cache_root) if cache_root else PROJECT_ROOT / "data" / "reference" / "group_analytics" / "rotation"
    shared = CONFIG.abs_path(CONFIG.data.raw_dir) / "ohlcv"
    frames = {}
    for symbol in symbols:
        path = root / f"{symbol}.parquet"
        if refresh:
            if fetcher is None:
                from src.data.fmp import get_historical_ohlcv
                fetcher = get_historical_ohlcv
            frame = fetcher(symbol, start, end, dividend_adjusted=True)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                root.mkdir(parents=True, exist_ok=True)
                # Unique atomic temp path prevents concurrent refresh corruption.
                from uuid import uuid4
                temp = root / f".{symbol}.{uuid4().hex}.tmp"
                try:
                    frame.to_parquet(temp)
                    temp.replace(path)
                finally:
                    temp.unlink(missing_ok=True)
        candidate = path if path.exists() else shared / f"{symbol}.parquet"
        if candidate.exists():
            frames[symbol] = pd.read_parquet(candidate)
    return frames


def run_rotation(*, asof="latest", refresh=False, store=None, frames=None, themes=None,
                 now=None, calendar=None, dry_run=False, amount_verified=False,
                 observations=(), decision_cutoff=None, cache_root=None):
    store = store or RotationStore()
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    latest = latest_completed_session(now=now, calendar=calendar)
    target = latest if asof == "latest" else pd.Timestamp(asof)
    target = target.tz_localize(None).normalize()
    close = official_session_close(target, calendar=calendar)
    if close > now or target > latest:
        raise ValueError("Only completed sessions are allowed")
    source_session = target.date().isoformat()
    # Historical runs default to that day's close, never today's revised macro.
    cutoff = pd.Timestamp(decision_cutoff) if decision_cutoff else close
    if cutoff.tzinfo is None or cutoff < close or cutoff > now:
        raise ValueError("Invalid evidence decision cutoff")
    try:
        cal = _calendar(calendar)
        sessions = cal.sessions_in_range((target - pd.Timedelta(days=550)).date().isoformat(), source_session)
        sessions = pd.DatetimeIndex(sessions).tz_localize(None)[-300:]
        themes = tuple(themes or default_themes())
        if len({t.id for t in themes}) != len(themes):
            raise ValueError("Duplicate theme ids")
        symbols = required_symbols(themes)
        if frames is None:
            frames = load_frames(symbols, sessions[0].date().isoformat(), source_session,
                                 refresh=refresh, cache_root=cache_root)
        prices, volumes = {}, {}
        for symbol, frame in frames.items():
            if symbol not in symbols or not isinstance(frame, pd.DataFrame):
                continue
            prices[symbol] = frame.get("adj_close", frame.get("close", pd.Series(dtype=float)))
            volumes[symbol] = frame.get("volume", pd.Series(dtype=float))
        prices = clean_table(pd.DataFrame(prices), sessions)
        volumes = clean_table(pd.DataFrame(volumes), sessions)
        rows = analyze(prices, volumes, sessions, themes,
                       amount_verified=amount_verified)
        valid = sum(bool(row["production"]["history_valid"]) for row in rows)
        if not valid:
            raise ValueError("No theme has 61 consecutive completed sessions")
        context = evaluate_context(observations, cutoff=cutoff.isoformat())
        for row in rows:
            row["price_response"] = (price_response(context, row["production"])
                                     if context.get("target_benchmark") in {None, row["benchmark"]}
                                     else "背景证据不适用于此基准，独立观察价格")
        input_panel = normalize_json_value({
            "sessions": sessions.strftime("%Y-%m-%d").tolist(),
            "price_columns": prices.columns.tolist(), "volume_columns": volumes.columns.tolist(),
            "prices": prices.to_numpy().tolist(), "volumes": volumes.to_numpy().tolist(),
        })
        fingerprint = hashlib.sha256(encoded(input_panel)).hexdigest()
        snapshot = normalize_json_value({
            "schema_version": SCHEMA_VERSION, "source_session": source_session,
            "generated_at": now.isoformat(), "decision_cutoff": cutoff.isoformat(),
            "profiles": [SOURCE_PROFILE, PRODUCTION_PROFILE], "price_basis": "adjusted_close",
            "amount_verified": amount_verified, "input_fingerprint": fingerprint,
            "input_panel": input_panel,
            "parameters": {"history_sessions": 300, "return_windows": [1, 5, 20, 60],
                           "relative_ma": [20, 50], "amount_ma": 20,
                           "source_volume_threshold": 1.3, "extension_rs60_pct": 18,
                           "extension_ma50_pct": 8, "confirmation_bars": 2,
                           "strength_deadband_log": .005, "acceleration_deadband_log": .002,
                           "min_breadth_members": 5, "min_breadth_coverage": .8,
                           "priority_breadth_pct": 60, "price_state_version": "log-quadrants-v1"},
            "session_status": "FINAL", "valid_theme_count": valid,
            "total_theme_count": len(rows), "context": context, "rows": rows,
            "notes": ["日线研究观察，不是交易指令；未包含实时盘前行情",
                      "公开版为规则级对照，未完成TradingView数值对账；评分未经样本外验证",
                      "自建篮子历史按固定成员回看，不是历史时点可选组合"],
        })
        run_id = None if dry_run else store.publish(snapshot)
        return {**snapshot, "run_id": run_id}
    except Exception as exc:
        if not dry_run:
            store.failure(source_session, type(exc).__name__)
        raise
