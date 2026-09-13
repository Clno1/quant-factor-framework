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
BREADTH_MA_SESSIONS = 20
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


class HoldingsValidationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _non_equity_kind(row):
    """Conservative provider evidence, not a general company-name classifier."""
    asset = str(row.get("asset") or "").upper()
    name = str(row.get("name") or "").strip().upper()
    cusip = str(row.get("securityCusip") or "").upper()
    if (cusip.startswith("ADI") and re.search(r"(?:MAR|JUN|SEP|DEC)\d{2}$", name)):
        return "derivative"
    if not asset:
        if name in {"CASH", "USD CASH", "-USD CASH-", "OTHER/CASH", "US DOLLAR", "EURO",
                    "SWISS FRANC", "JAPANESE YEN", "CANADIAN DOLLAR", "TAIWAN DOLLAR",
                    "KOREAN WON", "POUND STERLING", "CHINESE YUAN RENMINBI",
                    "HONG KONG DOLLAR", "AUSTRALIAN DOLLAR", "SINGAPORE DOLLAR"}:
            return "cash_fx"
        if name == "OTHER PAYABLE & RECEIVABLES" or name.startswith("CASH COLLATERAL "):
            return "cash_accounting"
        if cusip in {"924QSGII3", "066922477"}:
            return "money_market"
        if re.search(r"E-MINI.*(?:MAR|JUN|SEP|DEC)\d{2}$", name):
            return "derivative"
    return None


def normalize_observation(rows, symbol, captured_at, *, securities=None, reference_id=None):
    """Normalize account rows without treating excluded balances as equities.

    Scheduled ingestion MUST supply the bound securities lookup. None preserves
    standalone/legacy research compatibility and is explicitly labelled as such.
    Neither policy makes these observations historical PIT data.
    """
    captured = pd.Timestamp(captured_at)
    if captured.tzinfo is None or not isinstance(rows, list) or not rows:
        raise HoldingsValidationError("EMPTY_OR_UNDATED_HOLDINGS")
    members, excluded, seen = [], [], set()
    for row in rows:
        if row.get("symbol") != symbol:
            raise HoldingsValidationError("HOLDING_FUND_MISMATCH")
        asset = str(row.get("asset") or "")
        try:
            weight = float(row["weightPercentage"])
        except (KeyError, TypeError, ValueError):
            raise HoldingsValidationError("MISSING_HOLDING_WEIGHT") from None
        if not math.isfinite(weight) or abs(weight) > 100:
            raise HoldingsValidationError("INVALID_HOLDING_WEIGHT")
        identity = str(row.get("isin") or row.get("securityCusip") or "")
        kind = _non_equity_kind(row)
        if kind:
            excluded.append({"asset": asset, "name": str(row.get("name") or ""),
                             "weight_pct": weight, "reason": kind})
            continue
        if weight < 0:
            raise HoldingsValidationError("NEGATIVE_EQUITY_OR_UNRESOLVED_WEIGHT")
        # Cash/derivatives and exchange-suffixed listings are not silently
        # mapped onto US equity sessions. Retain them in excluded evidence.
        if not identity or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,14}", asset):
            excluded.append({"asset": asset, "name": str(row.get("name") or ""),
                             "weight_pct": weight, "reason": "UNRESOLVED_LISTING"})
            continue
        security_id = identity
        if securities is not None:
            match = securities.get(asset)
            if match is None:
                excluded.append({"asset": asset, "weight_pct": weight, "reason": "UNRESOLVED_US_EQUITY"})
                continue
            pairs = [(str(row.get(k) or ""), str(match.get(m) or ""))
                     for k, m in (("isin", "isin"), ("securityCusip", "cusip"))]
            comparable = [(a, b) for a, b in pairs if a and b]
            if not comparable or any(a != b for a, b in comparable):
                excluded.append({"asset": asset, "weight_pct": weight, "reason": "IDENTITY_UNRESOLVED_OR_CONFLICT"})
                continue
            security_id = match["security_id"]
        if security_id in seen or asset in {m["ticker"] for m in members}:
            raise HoldingsValidationError("DUPLICATE_EQUITY_IDENTITY")
        seen.add(security_id)
        members.append({"ticker": asset, "security_id": security_id, "weight_pct": weight})
    if not members:
        raise HoldingsValidationError("NO_RESOLVABLE_EQUITY_HOLDINGS")
    total = sum(m["weight_pct"] for m in members) + sum(x["weight_pct"] for x in excluded)
    if not 95 <= total <= 105:
        raise HoldingsValidationError("INCONSISTENT_TOTAL_HOLDING_WEIGHT")
    return {"schema_version": HOLDINGS_SCHEMA, "etf": symbol,
            "source": "FMP /stable/etf/holdings", "captured_at": captured.isoformat(),
            "provider_updated_at_raw": sorted({str(r.get("updatedAt") or "") for r in rows}),
            "holdings_effective_at": None, "point_in_time": False,
            "status": "OBSERVATION_ONLY_NO_PROVIDER_DATE",
            "identity_policy": "BOUND_SECURITY_MASTER" if securities is not None else "PROVIDER_ONLY_LEGACY",
            "normalization_version": "holdings-identity-v2",
            "security_master_generation_id": reference_id,
            "response_sha256": hashlib.sha256(encoded(rows)).hexdigest(),
            "members": members, "excluded": excluded, "reported_weight_pct": total,
            "excluded_gross_weight_pct": sum(abs(x["weight_pct"]) for x in excluded),
            "negative_excluded_weight_pct": sum(x["weight_pct"] for x in excluded if x["weight_pct"] < 0)}


def _member_price_series(frame):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    if "adj_close" in frame.columns:
        return frame["adj_close"]
    if "close" in frame.columns:
        return frame["close"]
    return None


def _normalized_member_series(frame):
    """Return a unique-session price series, or a rejection reason."""
    if not isinstance(frame, pd.DataFrame):
        return None, "NO_FRAME"
    prices = _member_price_series(frame)
    if prices is None or prices.empty:
        return None, "NO_PRICE_SERIES"
    aligned = prices.copy()
    aligned.index = pd.to_datetime(aligned.index, errors="coerce")
    if getattr(aligned.index, "tz", None) is not None:
        aligned.index = aligned.index.tz_localize(None)
    aligned.index = aligned.index.normalize()
    if aligned.index.hasnans or aligned.index.has_duplicates:
        return None, "DUPLICATE_OR_INVALID_SESSIONS"
    aligned = pd.to_numeric(aligned, errors="coerce")
    aligned = aligned.replace([float("inf"), float("-inf")], pd.NA)
    return aligned, None


def observation_breadth(observation, frames, sessions):
    """Evaluate member MA20 on a completed price date, not on a claimed holding date."""
    if observation.get("status") != "OBSERVATION_ONLY_NO_PROVIDER_DATE":
        raise ValueError("Unsupported holdings observation")
    if len(sessions) < BREADTH_MA_SESSIONS:
        raise ValueError("Need 20 exchange sessions")
    symbols = [m["ticker"] for m in observation["members"]]
    series = {}
    for symbol in symbols:
        aligned, _reason = _normalized_member_series((frames or {}).get(symbol))
        if aligned is not None:
            series[symbol] = aligned
    try:
        if series:
            table = clean_table(pd.DataFrame(series), sessions)
        else:
            table = pd.DataFrame(index=pd.DatetimeIndex(sessions), columns=symbols, dtype=float)
    except (ValueError, TypeError, KeyError):
        table = pd.DataFrame(index=pd.DatetimeIndex(sessions), columns=symbols, dtype=float)
    table = table.reindex(columns=symbols).where(lambda f: f > 0)
    ma = table.rolling(BREADTH_MA_SESSIONS, min_periods=BREADTH_MA_SESSIONS).mean().iloc[-1]
    last = table.iloc[-1]
    valid = last.notna() & ma.notna()
    above = last.gt(ma) & valid
    weights = {m["ticker"]: m["weight_pct"] for m in observation["members"]}
    expected = len(symbols)
    n = int(valid.sum())
    total_weight = sum(weights.values())
    valid_weight = sum(weights[s] for s in symbols if valid[s])
    excluded_weight = sum(float(item.get("weight_pct") or 0) for item in observation.get("excluded") or [])
    return {"etf": observation["etf"], "price_session": sessions[-1].date().isoformat(),
            "holdings_captured_at": observation["captured_at"], "holdings_effective_at": None,
            "status": "OBSERVATION_ONLY_NO_PROVIDER_DATE", "point_in_time": False,
            "eligible_members": n, "mapped_equity_members": expected,
            "member_coverage": n / expected, "mapped_equity_weight_pct": total_weight,
            "excluded_weight_pct": excluded_weight,
            "reported_weight_pct": total_weight + excluded_weight,
            "valid_weight_pct": valid_weight,
            "measured_fund_weight_pct": valid_weight,
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
            "normalized_sha256": hashlib.sha256(encoded({key: observation.get(key) for key in
                ("members", "excluded", "identity_policy", "security_master_generation_id")})).hexdigest(),
        })
    return hashlib.sha256(encoded(payload)).hexdigest() if payload else None


def member_measurement_fingerprint(frames, sessions):
    """Hash the same 20-session window observation_breadth uses for MA20."""
    if not frames or sessions is None or len(sessions) == 0:
        return None
    series = {}
    rejected = []
    for symbol, frame in frames.items():
        aligned, reason = _normalized_member_series(frame)
        if aligned is None:
            rejected.append({"symbol": str(symbol), "reason": reason or "UNUSABLE"})
        else:
            series[str(symbol)] = aligned
    window = None
    try:
        if series:
            table = clean_table(pd.DataFrame(series), sessions).where(lambda frame: frame > 0)
            window = table.iloc[-BREADTH_MA_SESSIONS:]
            session_ids = [pd.Timestamp(idx).date().isoformat() for idx in window.index]
        else:
            tail = list(sessions[-BREADTH_MA_SESSIONS:]) if len(sessions) >= BREADTH_MA_SESSIONS else list(sessions)
            session_ids = [pd.Timestamp(idx).date().isoformat() for idx in tail]
    except (ValueError, TypeError, KeyError):
        rejected.extend({"symbol": symbol, "reason": "BREADTH_TABLE_INVALID"} for symbol in series)
        window = None
        tail = list(sessions[-BREADTH_MA_SESSIONS:]) if len(sessions) >= BREADTH_MA_SESSIONS else list(sessions)
        session_ids = [pd.Timestamp(idx).date().isoformat() for idx in tail]
    members = []
    for symbol in sorted(map(str, frames)):
        if window is not None and symbol in window.columns:
            values, missing = [], []
            for raw in window[symbol].tolist():
                number = _finite(raw)
                values.append(number)
                missing.append(number is None)
        else:
            values = [None] * len(session_ids)
            missing = [True] * len(session_ids)
        members.append({"symbol": symbol, "values": values, "missing": missing})
    return hashlib.sha256(encoded({
        "sessions": session_ids,
        "members": members,
        "rejected": sorted(rejected, key=lambda item: item["symbol"]),
    })).hexdigest()


def format_holdings_breadth_text(overlay):
    equal = _finite((overlay or {}).get("breadth_equal_weight_pct"))
    if equal is None:
        return "当前持仓观测 · 成员价格不足"
    weighted = _finite(overlay.get("breadth_weighted_pct"))
    fund = _finite(overlay.get("measured_fund_weight_pct"))
    text = f"样本内参与 等权{equal:.0f}%"
    if weighted is not None:
        text += f" / 加权{weighted:.0f}%"
    eligible = overlay.get("breadth_eligible_members")
    mapped = overlay.get("breadth_mapped_members")
    if eligible is not None and mapped is not None:
        text += f" · 样本{eligible}/{mapped}"
    if fund is not None:
        text += f" · 已测基金权重{fund:.0f}%"
    if overlay.get("measurement_complete") is False:
        text += " · 仅部分持仓观察"
    return text


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
        "mapped_equity_weight_pct": None,
        "measured_fund_weight_pct": None,
        "excluded_weight_pct": None,
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


def attach_holdings_breadth(rows, observations, frames, sessions, *, now,
                            allow_current_observation=True):
    """Overlay dual-calibre observation onto rows; never writes production.breadth."""
    for row in rows:
        try:
            _attach_one_holdings_row(
                row, observations, frames, sessions, now=now,
                allow_current_observation=allow_current_observation,
            )
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


def _attach_one_holdings_row(row, observations, frames, sessions, *, now,
                             allow_current_observation=True):
    has_basket_members = bool(row.get("definition", {}).get("members"))
    if has_basket_members:
        overlay = _basket_overlay(row)
        row["holdings_breadth"] = overlay
        row["breadth_kind"] = "member_above_ma"
        return
    if not allow_current_observation:
        overlay = _etf_unavailable(
            "HOLDINGS_NOT_POINT_IN_TIME",
            "当前持仓观测不用于历史时点广度，禁止用今日名单配过去价格",
            ["HOLDINGS_NOT_POINT_IN_TIME"],
        )
        row["holdings_breadth"] = overlay
        row["breadth_kind"] = "unavailable"
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
    if equal is not None and not measured.get("measurement_complete"):
        gaps.append("PARTIAL_HOLDINGS_COVERAGE")
    overlay = {
        "breadth_kind": "etf_holdings_observation",
        "status": measured.get("status"),
        "breadth_equal_weight_pct": equal,
        "breadth_weighted_pct": _finite(measured.get("weighted_above_ma20_pct")),
        "breadth_eligible_members": measured.get("eligible_members"),
        "breadth_mapped_members": measured.get("mapped_equity_members"),
        "breadth_member_coverage": _finite(measured.get("member_coverage")),
        "breadth_weight_coverage": _finite(measured.get("weight_coverage")),
        "mapped_equity_weight_pct": _finite(measured.get("mapped_equity_weight_pct")),
        "measured_fund_weight_pct": _finite(measured.get("measured_fund_weight_pct")),
        "excluded_weight_pct": _finite(measured.get("excluded_weight_pct")),
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
