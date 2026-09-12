"""Current holdings observation only; never historical membership or a signal input."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import math
import re

import pandas as pd

from src.config import PROJECT_ROOT

from .engine import clean_table
from .store import encoded

HOLDINGS_SCHEMA = "rotation.holdings-observation.v1"
HOLDINGS_STALE_CALENDAR_DAYS = 14
HOLDINGS_NOTE = "当前持仓观测广度（持仓生效日未披露）"
STAMP_NAME = re.compile(r"^[0-9]{8}T[0-9]{6}Z\.json$")
SAFE_ETF = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
UNLINKED_WARNING = "ETF真实持仓广度未接入，不能将趋势代理当成成员比例"


def default_holdings_root():
    return PROJECT_ROOT / "data" / "reference" / "group_analytics" / "rotation" / "holdings"


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_observation(rows, symbol, captured_at):
    captured = pd.Timestamp(captured_at)
    if captured.tzinfo is None or not isinstance(rows, list) or not rows:
        raise ValueError("Need dated nonempty holdings observation")
    members, excluded, seen = [], [], set()
    for row in rows:
        if row.get("symbol") != symbol:
            raise ValueError("Holding fund mismatch")
        asset = str(row.get("asset") or "")
        try:
            weight = float(row["weightPercentage"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Missing holding weight") from None
        if not math.isfinite(weight) or weight < 0 or weight > 100:
            raise ValueError("Invalid holding weight")
        identity = str(row.get("isin") or row.get("securityCusip") or "")
        if asset in seen:
            raise ValueError("Duplicate/ambiguous holding symbol")
        seen.add(asset)
        # Cash/derivatives and exchange-suffixed listings are not silently
        # mapped onto US equity sessions. Retain them in excluded evidence.
        if not identity or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,14}", asset):
            excluded.append({"asset": asset, "weight_pct": weight, "reason": "cash_or_unresolved_listing"})
            continue
        members.append({"ticker": asset, "security_id": identity, "weight_pct": weight})
    if not members:
        raise ValueError("No resolvable equity holdings")
    total = sum(m["weight_pct"] for m in members) + sum(x["weight_pct"] for x in excluded)
    if not 95 <= total <= 105:
        raise ValueError("Holding weights suggest partial or inconsistent response")
    return {"schema_version": HOLDINGS_SCHEMA, "etf": symbol,
            "source": "FMP /stable/etf/holdings", "captured_at": captured.isoformat(),
            "provider_updated_at_raw": sorted({str(r.get("updatedAt") or "") for r in rows}),
            "holdings_effective_at": None, "point_in_time": False,
            "status": "OBSERVATION_ONLY_NO_PROVIDER_DATE",
            "response_sha256": hashlib.sha256(encoded(rows)).hexdigest(),
            "members": members, "excluded": excluded, "reported_weight_pct": total}


def _member_price_series(frame):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    if "adj_close" in frame.columns:
        return frame["adj_close"]
    if "close" in frame.columns:
        return frame["close"]
    return None


def observation_breadth(observation, frames, sessions):
    """Evaluate member MA20 on a completed price date, not on a claimed holding date."""
    if observation.get("status") != "OBSERVATION_ONLY_NO_PROVIDER_DATE":
        raise ValueError("Unsupported holdings observation")
    if len(sessions) < 20:
        raise ValueError("Need 20 exchange sessions")
    symbols = [m["ticker"] for m in observation["members"]]
    series = {}
    for symbol, frame in (frames or {}).items():
        if symbol not in symbols:
            continue
        prices = _member_price_series(frame)
        if prices is not None:
            series[symbol] = prices
    table = clean_table(pd.DataFrame(series), sessions)
    table = table.reindex(columns=symbols).where(lambda f: f > 0)
    ma = table.rolling(20, min_periods=20).mean().iloc[-1]
    last = table.iloc[-1]
    valid = last.notna() & ma.notna()
    above = last.gt(ma) & valid
    weights = {m["ticker"]: m["weight_pct"] for m in observation["members"]}
    expected = len(symbols)
    n = int(valid.sum())
    total_weight = sum(weights.values())
    valid_weight = sum(weights[s] for s in symbols if valid[s])
    return {"etf": observation["etf"], "price_session": sessions[-1].date().isoformat(),
            "holdings_captured_at": observation["captured_at"], "holdings_effective_at": None,
            "status": "OBSERVATION_ONLY_NO_PROVIDER_DATE", "point_in_time": False,
            "eligible_members": n, "mapped_equity_members": expected,
            "member_coverage": n / expected, "mapped_equity_weight_pct": total_weight,
            "valid_weight_pct": valid_weight,
            "weight_coverage": valid_weight / total_weight if total_weight else None,
            "above_ma20_pct": 100 * int(above.sum()) / n if n else None,
            "weighted_above_ma20_pct": (
                100 * sum(weights[s] for s in symbols if above[s]) / valid_weight if valid_weight else None
            ),
            "measurement_complete": n >= 5 and n / expected >= .8 and valid_weight >= 80,
            "production_eligible": False,
            "note": "当前持仓观测 × 指定收盘日价格；持仓生效日未披露，不进入历史回测、生产广度确认或分数"}


def stamp_from_captured(captured_at):
    captured = pd.Timestamp(captured_at)
    if captured.tzinfo is None:
        raise ValueError("Holdings captured_at must be timezone-aware")
    return captured.tz_convert("UTC").strftime("%Y%m%dT%H%M%SZ") + ".json"


def observation_age_days(captured_at, now):
    captured = pd.Timestamp(captured_at)
    current = pd.Timestamp(now)
    if captured.tzinfo is None or current.tzinfo is None:
        raise ValueError("Holdings timestamps must be timezone-aware")
    return int((current.tz_convert("UTC").normalize() - captured.tz_convert("UTC").normalize()).days)


def is_observation_stale(observation, now, *, max_age_days=HOLDINGS_STALE_CALENDAR_DAYS):
    return observation_age_days(observation["captured_at"], now) > max_age_days


def save_observation(root, observation):
    symbol = observation.get("etf")
    if not isinstance(symbol, str) or not SAFE_ETF.fullmatch(symbol):
        raise ValueError("Invalid ETF symbol")
    if observation.get("schema_version") != HOLDINGS_SCHEMA:
        raise ValueError("Unsupported holdings observation schema")
    from ..adapters import _atomic_json
    path = Path(root) / symbol / stamp_from_captured(observation["captured_at"])
    _atomic_json(path, observation)
    return path


def load_latest_observation(etf_dir):
    directory = Path(etf_dir)
    if not directory.is_dir():
        return None
    files = sorted(
        path for path in directory.iterdir()
        if path.is_file() and not path.is_symlink() and STAMP_NAME.fullmatch(path.name)
    )
    if not files:
        return None
    payload = json.loads(files[-1].read_text())
    if not isinstance(payload, dict) or payload.get("schema_version") != HOLDINGS_SCHEMA:
        raise ValueError("Unsupported holdings observation schema")
    expected = directory.name
    if payload.get("etf") != expected:
        raise ValueError("Holding fund mismatch")
    return payload


def load_latest_observations(root, symbols):
    result = {}
    base = Path(root)
    if not base.is_dir():
        return result
    for symbol in symbols:
        if not isinstance(symbol, str) or not SAFE_ETF.fullmatch(symbol):
            continue
        try:
            observation = load_latest_observation(base / symbol)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if observation:
            result[symbol] = observation
    return result


def holding_member_symbols(observations):
    seen, ordered = set(), []
    for observation in observations.values():
        for member in observation.get("members") or []:
            ticker = member.get("ticker")
            if ticker and ticker not in seen:
                seen.add(ticker)
                ordered.append(ticker)
    return ordered


def holdings_fingerprint(observations, *, now=None):
    payload = []
    for symbol in sorted(observations):
        observation = observations[symbol]
        stale = bool(now is not None and is_observation_stale(observation, now))
        payload.append({
            "symbol": symbol,
            "response_sha256": observation.get("response_sha256"),
            "captured_at": observation.get("captured_at"),
            "applied": not stale,
            "stale": stale,
        })
    return hashlib.sha256(encoded(payload)).hexdigest() if payload else None


def _empty_overlay(*, kind, status, note, gaps=(), **extra):
    payload = {
        "breadth_kind": kind,
        "status": status,
        "breadth_equal_weight_pct": None,
        "breadth_weighted_pct": None,
        "breadth_eligible_members": None,
        "breadth_mapped_members": None,
        "breadth_member_coverage": None,
        "breadth_weight_coverage": None,
        "holdings_captured_at": None,
        "holdings_effective_at": None,
        "point_in_time": False,
        "measurement_complete": False,
        "production_eligible": False,
        "observation_gaps": list(gaps),
        "note": note,
    }
    payload.update(extra)
    return payload


def _basket_overlay(row):
    production = row.get("production") or {}
    return _empty_overlay(
        kind="member_above_ma",
        status="basket_members",
        note="自建篮子固定成员等权站上20日线，不是ETF持仓观测",
        breadth_equal_weight_pct=_finite(production.get("breadth")),
        breadth_eligible_members=production.get("breadth_n"),
        breadth_mapped_members=production.get("breadth_expected"),
        breadth_member_coverage=(
            None if not production.get("breadth_expected") else
            _finite(production.get("breadth_n")) / production["breadth_expected"]
            if _finite(production.get("breadth_n")) is not None else None
        ),
    )


def _etf_unavailable(status, note, gaps):
    return _empty_overlay(kind="unavailable", status=status, note=note, gaps=gaps)


def attach_holdings_breadth(rows, observations, frames, sessions, *, now):
    """Overlay dual-calibre observation onto rows; never writes production.breadth."""
    for row in rows:
        try:
            _attach_one_holdings_row(row, observations, frames, sessions, now=now)
        except Exception:
            if "holdings_breadth" not in row:
                row["holdings_breadth"] = _etf_unavailable(
                    "HOLDINGS_MEASUREMENT_FAILED",
                    HOLDINGS_NOTE + "；持仓观测叠加失败",
                    ["HOLDINGS_MEASUREMENT_FAILED"],
                )
                if not row.get("breadth_kind"):
                    row["breadth_kind"] = "unavailable"
    return rows


def _attach_one_holdings_row(row, observations, frames, sessions, *, now):
    has_basket_members = bool(row.get("definition", {}).get("members"))
    if has_basket_members:
        overlay = _basket_overlay(row)
        row["holdings_breadth"] = overlay
        row["breadth_kind"] = "member_above_ma"
        return
    proxy = row.get("proxy")
    observation = observations.get(proxy) if proxy else None
    if observation is None:
        overlay = _etf_unavailable(
            "ETF_HOLDINGS_NOT_LINKED",
            "ETF真实持仓广度未接入，不能将趋势代理当成成员比例",
            ["ETF_HOLDINGS_NOT_LINKED"],
        )
        row["holdings_breadth"] = overlay
        row["breadth_kind"] = "unavailable"
        return
    if is_observation_stale(observation, now):
        overlay = _etf_unavailable(
            "HOLDINGS_OBSERVATION_STALE",
            "持仓观测超过14个日历日，已停用，不使用旧持仓",
            ["HOLDINGS_OBSERVATION_STALE"],
        )
        overlay["holdings_captured_at"] = observation.get("captured_at")
        row["holdings_breadth"] = overlay
        row["breadth_kind"] = "unavailable"
        return
    try:
        measured = observation_breadth(observation, frames or {}, sessions)
    except Exception:
        overlay = _etf_unavailable(
            "HOLDINGS_MEASUREMENT_FAILED",
            HOLDINGS_NOTE + "；成员价格不足以完成测量",
            ["HOLDINGS_MEASUREMENT_FAILED"],
        )
        overlay["holdings_captured_at"] = observation.get("captured_at")
        overlay["breadth_kind"] = "etf_holdings_observation"
        row["holdings_breadth"] = overlay
        row["breadth_kind"] = "etf_holdings_observation"
        _mark_holdings_linked(row)
        return
    equal = _finite(measured.get("above_ma20_pct"))
    gaps = []
    if equal is not None and equal < 60:
        gaps.append("LOW_PARTICIPATION")
    overlay = {
        "breadth_kind": "etf_holdings_observation",
        "status": measured.get("status"),
        "breadth_equal_weight_pct": equal,
        "breadth_weighted_pct": _finite(measured.get("weighted_above_ma20_pct")),
        "breadth_eligible_members": measured.get("eligible_members"),
        "breadth_mapped_members": measured.get("mapped_equity_members"),
        "breadth_member_coverage": _finite(measured.get("member_coverage")),
        "breadth_weight_coverage": _finite(measured.get("weight_coverage")),
        "holdings_captured_at": measured.get("holdings_captured_at"),
        "holdings_effective_at": measured.get("holdings_effective_at"),
        "point_in_time": False,
        "measurement_complete": bool(measured.get("measurement_complete")),
        "production_eligible": False,
        "excluded": list(observation.get("excluded") or []),
        "observation_gaps": gaps,
        "note": HOLDINGS_NOTE,
    }
    row["holdings_breadth"] = overlay
    row["breadth_kind"] = "etf_holdings_observation"
    _mark_holdings_linked(row)


def _mark_holdings_linked(row):
    warnings = [item for item in (row.get("warnings") or []) if item != UNLINKED_WARNING]
    if HOLDINGS_NOTE not in warnings:
        warnings.append(HOLDINGS_NOTE)
    row["warnings"] = warnings
    # Display gaps live on holdings_breadth. Engine gaps stay on production so
    # replay MATCH and attach_candidates do not treat observation as a signal.
