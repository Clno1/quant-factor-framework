"""CLI-only rotation pipeline. Provider reads never occur in Web requests."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import hashlib
import json

import pandas as pd

from src.config import PROJECT_ROOT
from ..calendar import _calendar, latest_completed_session, official_session_close
from ..artifacts import normalize_json_value
from . import (
    AMOUNT_AUDIT_DOC,
    AMOUNT_BASIS,
    CACHE_PRICE_BASIS,
    FLOWS_AUDIT_DOC,
    FLOWS_AUDIT_STATUS,
    HOLDINGS_AUDIT_DOC,
    PRODUCTION_PROFILE,
    SCHEMA_VERSION,
    SOURCE_PROFILE,
)
from .context import evaluate_context, price_response
from .engine import analyze, clean_table
from .flows import attach_net_creation, flows_fingerprint
from .holdings import (
    attach_holdings_breadth,
    default_holdings_root,
    holding_member_symbols,
    holdings_fingerprint,
    load_latest_observations,
)
from .store import RotationStore, encoded
from .themes import default_themes, proxy_etf_symbols, required_symbols


def _canonical_cache_root(cache_root=None):
    parent = Path(cache_root) if cache_root else PROJECT_ROOT / "data" / "reference" / "group_analytics" / "rotation"
    return parent / "canonical"


def _basis_sidecar(parquet_path: Path) -> Path:
    return parquet_path.with_name(parquet_path.stem + ".basis.json")


def _price_basis_ok(sidecar: Path) -> bool:
    if not sidecar.exists():
        return False
    try:
        meta = json.loads(sidecar.read_text())
    except (OSError, ValueError, TypeError):
        return False
    return meta.get("price_basis") == CACHE_PRICE_BASIS


def _write_basis(sidecar: Path):
    sidecar.write_text(json.dumps({
        "price_basis": CACHE_PRICE_BASIS,
        "amount_basis": AMOUNT_BASIS,
    }, ensure_ascii=False, sort_keys=True))


def load_frames(symbols, start, end, *, refresh=False, cache_root=None, fetcher=None):
    """Group-owned canonical cache. Shared raw OHLCV is never a silent fallback."""
    root = _canonical_cache_root(cache_root)
    frames = {}
    for symbol in symbols:
        path = root / f"{symbol}.parquet"
        sidecar = _basis_sidecar(path)
        if refresh:
            if fetcher is None:
                from src.data.fmp import get_canonical_historical_ohlcv
                fetcher = get_canonical_historical_ohlcv
            frame = fetcher(symbol, start, end)
            if isinstance(frame, pd.DataFrame) and not frame.empty:
                root.mkdir(parents=True, exist_ok=True)
                temp = root / f".{symbol}.{uuid4().hex}.tmp"
                try:
                    frame.to_parquet(temp)
                    temp.replace(path)
                finally:
                    temp.unlink(missing_ok=True)
                _write_basis(sidecar)
        if path.exists() and _price_basis_ok(sidecar):
            frames[symbol] = pd.read_parquet(path)
    return frames


def run_rotation(*, asof="latest", refresh=False, store=None, frames=None, themes=None,
                 now=None, calendar=None, dry_run=False, amount_verified=True,
                 observations=(), decision_cutoff=None, cache_root=None,
                 holdings_root=None):
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
        prices, volumes, execution = {}, {}, {}
        for symbol, frame in frames.items():
            if symbol not in symbols or not isinstance(frame, pd.DataFrame):
                continue
            prices[symbol] = frame.get("adj_close", frame.get("close", pd.Series(dtype=float)))
            volumes[symbol] = frame.get("volume", pd.Series(dtype=float))
            execution[symbol] = frame.get("close", frame.get("adj_close", pd.Series(dtype=float)))
        prices = clean_table(pd.DataFrame(prices), sessions)
        volumes = clean_table(pd.DataFrame(volumes), sessions)
        exec_px = clean_table(pd.DataFrame(execution), sessions)
        rows = analyze(prices, volumes, sessions, themes,
                       amount_verified=amount_verified, execution_close=exec_px,
                       schema_version=SCHEMA_VERSION)
        holdings_observations = {}
        try:
            observed_root = Path(holdings_root) if holdings_root is not None else default_holdings_root()
            holdings_observations = load_latest_observations(observed_root, proxy_etf_symbols(themes))
        except Exception:
            holdings_observations = {}
        member_frames = {}
        try:
            members = holding_member_symbols(holdings_observations)
            if members:
                member_frames = load_frames(
                    members, sessions[0].date().isoformat(), source_session,
                    refresh=False, cache_root=cache_root,
                )
                if frames is not None:
                    for symbol in members:
                        frame = frames.get(symbol)
                        if symbol not in member_frames and isinstance(frame, pd.DataFrame):
                            member_frames[symbol] = frame
        except Exception:
            member_frames = {}
        attach_holdings_breadth(rows, holdings_observations, member_frames, sessions, now=now)
        attach_net_creation(rows)
        valid = sum(bool(row["production"]["history_valid"]) for row in rows)
        if not valid:
            raise ValueError("No theme has 61 consecutive completed sessions")
        context = evaluate_context(observations, cutoff=cutoff.isoformat())
        for row in rows:
            row["price_response"] = (price_response(context, row["production"])
                                     if context.get("target_benchmark") in {None, row["benchmark"]}
                                     else "背景证据不适用于此基准，独立观察价格")
        holdings_fp = holdings_fingerprint(holdings_observations, now=now)
        flows_fp = flows_fingerprint(rows)
        input_panel = normalize_json_value({
            "sessions": sessions.strftime("%Y-%m-%d").tolist(),
            "price_columns": prices.columns.tolist(), "volume_columns": volumes.columns.tolist(),
            "execution_close_columns": exec_px.columns.tolist(),
            "prices": prices.to_numpy().tolist(), "volumes": volumes.to_numpy().tolist(),
            "execution_close": exec_px.to_numpy().tolist(),
            "holdings_fingerprint": holdings_fp, "flows_fingerprint": flows_fp,
        })
        fingerprint = hashlib.sha256(encoded(input_panel)).hexdigest()
        snapshot = normalize_json_value({
            "schema_version": SCHEMA_VERSION, "source_session": source_session,
            "generated_at": now.isoformat(), "decision_cutoff": cutoff.isoformat(),
            "profiles": [SOURCE_PROFILE, PRODUCTION_PROFILE], "price_basis": CACHE_PRICE_BASIS,
            "amount_basis": AMOUNT_BASIS, "amount_audit_doc": AMOUNT_AUDIT_DOC,
            "holdings_audit_doc": HOLDINGS_AUDIT_DOC, "flows_audit_doc": FLOWS_AUDIT_DOC,
            "flows_audit_status": FLOWS_AUDIT_STATUS, "amount_verified": amount_verified,
            "input_fingerprint": fingerprint, "holdings_fingerprint": holdings_fp,
            "flows_fingerprint": flows_fp, "input_panel": input_panel,
            "parameters": {"history_sessions": 300, "return_windows": [1, 5, 20, 60],
                           "relative_ma": [20, 50], "amount_ma": 20,
                           "source_volume_threshold": 1.3, "extension_rs60_pct": 18,
                           "extension_ma50_pct": 8, "confirmation_bars": 2,
                           "strength_deadband_log": .005, "acceleration_deadband_log": .002,
                           "min_breadth_members": 5, "min_breadth_coverage": .8,
                           "holdings_stale_calendar_days": 14,
                           "price_state_version": "dual-axis-v3"},
            "session_status": "FINAL", "valid_theme_count": valid,
            "total_theme_count": len(rows), "context": context, "rows": rows,
            "notes": ["日线研究观察，不是交易指令；未包含实时盘前行情",
                      "公开版为规则级对照，未完成TradingView数值对账；生产0–100分不在主表展示",
                      "自建篮子历史按固定成员回看，不是历史时点可选组合",
                      "成交额口径为拆股复权收盘价×成交量，见 " + AMOUNT_AUDIT_DOC,
                      "ETF真实广度为当前持仓观测，持仓生效日未披露，见 " + HOLDINGS_AUDIT_DOC,
                      "净申赎审计未通过，列为空且不用成交额冒充，见 " + FLOWS_AUDIT_DOC],
        })
        run_id = None if dry_run else store.publish(snapshot)
        return {**snapshot, "run_id": run_id}
    except Exception as exc:
        if not dry_run:
            store.failure(source_session, type(exc).__name__)
        raise
