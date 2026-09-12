"""Deterministic daily-bar profiles. Returns use percentage-number units.

Compatibility is a rule-level reference, not a claim of TradingView/provider
numerical parity. Production never consumes its fabricated ETF breadth.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import LEGACY_SCHEMA_VERSION, SCHEMA_VERSION
from .themes import Theme

STATE_NAMES = {
    "leading": "领先且加速", "cooling": "领先但降温",
    "improving": "落后但改善", "lagging": "落后且减速",
    "neutral": "边界观察", "pending": "等待连续确认", "unavailable": "数据不足",
}
SOURCE_NAMES = {9: "无数据", -2: "派发／撤出", 3: "拥挤主升", 2: "确认进入",
                1: "早期轮动", -1: "资金撤出", 0: "中性观察"}
STRENGTH_LABELS = {"leading": "领先", "flat": "持平", "lagging": "落后"}
SPEED_LABELS = {"accelerating": "加速", "steady": "平稳", "decelerating": "减速"}
COMBINED_LABELS = {
    ("leading", "accelerating"): "领先且加速",
    ("leading", "steady"): "稳定领先",
    ("leading", "decelerating"): "领先但降温",
    ("flat", "accelerating"): "持平转强",
    ("flat", "steady"): "与基准同步",
    ("flat", "decelerating"): "持平转弱",
    ("lagging", "accelerating"): "落后但改善",
    ("lagging", "steady"): "稳定落后",
    ("lagging", "decelerating"): "落后且减速",
}
PRIORITY_LABELS = {
    "unavailable": "数据不足",
    "focus": "持续领先",
    "defensive": "相对抗跌",
    "recover": "正在修复",
    "weak": "持续落后",
    "neutral": "与基准同步",
}
DX = 0.005
DY = 0.002


def clean_table(frame: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    result = frame.copy()
    result.index = pd.to_datetime(result.index)
    if result.index.tz is not None:
        result.index = result.index.tz_localize(None)
    result.index = result.index.normalize()
    if result.index.has_duplicates or result.columns.has_duplicates:
        raise ValueError("Duplicate sessions or symbols")
    return result.sort_index().reindex(sessions).apply(pd.to_numeric, errors="coerce").replace([np.inf, -np.inf], np.nan)


def _return(series, window, strict):
    value = 100 * (series / series.shift(window) - 1)
    if strict:
        value = value.where(series.notna().rolling(window + 1).sum() == window + 1)
    return value.replace([np.inf, -np.inf], np.nan)


def basket_index(prices: pd.DataFrame, *, strict: bool) -> pd.Series:
    returns = prices / prices.shift(1) - 1
    average = returns.mean(axis=1)
    values = []
    current = np.nan if strict else 100.0
    for j, (_, row) in enumerate(prices.iterrows()):
        if strict:
            if not row.notna().all():
                current = np.nan
            elif pd.isna(current):
                current = 100.0  # New segment; rolling validity forbids crossing gaps.
            elif returns.iloc[j].notna().all():
                current *= 1 + average.iloc[j]
            else:
                current = np.nan
        elif j > 0:
            current *= 1 + (0.0 if pd.isna(average.iloc[j]) else average.iloc[j])
        values.append(current)
    return pd.Series(values, index=prices.index, dtype=float)


def _source_state(row):
    if not row["valid"]:
        return 9
    if row["distribution"]:
        return -2
    if row["extension"] and row["score"] >= 60:
        return 3
    if row["score"] >= 75:
        return 2
    if row["score"] >= 60:
        return 1
    if row["score"] < 45 and row["rs5"] < 0 and row["rs20"] < 0:
        return -1
    return 0


def classify_axes(rs5, rs20):
    x = np.log1p(rs20 / 100)
    y = np.log1p(rs5 / 100) - (x - np.log1p(rs5 / 100)) / 3
    if not np.isfinite(x) or not np.isfinite(y):
        return x, y, "unavailable", "unavailable"
    strength = "flat" if abs(x) <= DX + 1e-12 else ("leading" if x > 0 else "lagging")
    speed = "steady" if abs(y) <= DY + 1e-12 else ("accelerating" if y > 0 else "decelerating")
    return float(x), float(y), strength, speed


def assign_priority(history_valid, strength, speed, abs20, index, absolute_ma20, abs5):
    if not history_valid or strength not in STRENGTH_LABELS:
        return "unavailable"
    if strength == "leading":
        if abs20 > 0 and index > absolute_ma20:
            return "focus"
        return "defensive"
    if strength == "lagging":
        if speed == "accelerating" and abs5 > 0:
            return "recover"
        return "weak"
    return "neutral"


def amount_direction_label(rs1, abs1, amount_ratio):
    if amount_ratio is None or not np.isfinite(amount_ratio):
        return None
    if amount_ratio >= 1.3:
        prefix = "放量"
    elif amount_ratio >= 1:
        prefix = "平量"
    else:
        prefix = "缩量"
    if rs1 is None or not np.isfinite(rs1):
        return prefix
    if rs1 > 0:
        relative = "相对走强"
    elif rs1 < 0:
        relative = "相对走弱"
    else:
        relative = "相对持平"
    label = prefix + relative
    if abs1 is not None and np.isfinite(abs1) and abs1 < 0:
        label += "（绝对下跌）"
    return label


def metric_frame(theme: Theme, prices: pd.DataFrame, volumes: pd.DataFrame,
                 *, strict: bool, amount_verified: bool = False,
                 execution_close: pd.DataFrame | None = None,
                 schema_version: str | None = None) -> pd.DataFrame:
    # NaN prices are never accepted as zero. Compatibility explicitly mimics
    # historical gaps_off mapping; production sees the original gaps.
    schema_version = schema_version or SCHEMA_VERSION
    prices = prices.where(prices > 0)
    volumes = volumes.where(volumes >= 0)
    exec_px = prices if execution_close is None else execution_close.where(execution_close > 0)
    if not strict:
        prices, volumes, exec_px = prices.ffill(), volumes.ffill(), exec_px.ffill()
    symbols = list(theme.members)
    member_prices = prices.reindex(columns=symbols)
    idx = prices[theme.proxy] if theme.proxy else basket_index(member_prices, strict=strict)
    benchmark = prices[theme.benchmark]
    q = (idx / benchmark).replace([np.inf, -np.inf], np.nan)
    result = pd.DataFrame({"index": idx, "ratio": q}, index=prices.index)
    for k in (1, 5, 20, 60):
        result[f"rs{k}"] = _return(q, k, strict)
        result[f"abs{k}"] = _return(idx, k, strict)
    ma20, ma50 = q.rolling(20).mean(), q.rolling(50).mean()
    result["relative_ma20"], result["relative_ma50"] = ma20, ma50
    result["absolute_ma20"] = idx.rolling(20).mean()
    result["dist50"] = 100 * (q / ma50 - 1)
    result["relative_ma20_slope5"] = ma20 - ma20.shift(5)
    result["previous_rs5"] = result.rs5.shift(5)
    result["valid"] = q.notna() & benchmark.notna()
    result["history_valid"] = q.notna().rolling(61).sum().eq(61)

    if symbols and (strict or not theme.proxy):
        ma = member_prices.rolling(20).mean()
        eligible = member_prices.notna() & ma.notna() if strict else member_prices.notna()
        n = eligible.sum(axis=1)
        result["breadth"] = (member_prices.gt(ma) & eligible).sum(axis=1).div(n.replace(0, np.nan)) * 100
        result["breadth_n"] = n
        result["breadth_expected"] = len(symbols)
    else:
        result["breadth"] = np.nan if strict else np.where(idx.isna(), np.nan, np.where(idx > idx.rolling(20).mean(), 70, 30))
        result["breadth_n"] = 0
        result["breadth_expected"] = 0
    # Compare identical membership sets across the two dates.
    if symbols and strict:
        common = eligible & eligible.shift(5, fill_value=False)
        result["breadth_change5"] = ((member_prices.gt(ma) & common).sum(axis=1) - (member_prices.gt(ma).shift(5, fill_value=False) & common).sum(axis=1)).div(common.sum(axis=1).replace(0, np.nan)) * 100
    else:
        result["breadth_change5"] = np.nan

    amount_symbols = [theme.proxy] if theme.proxy else symbols
    amount = exec_px.reindex(columns=amount_symbols) * volumes.reindex(columns=amount_symbols)
    dollars = amount.sum(axis=1, min_count=len(amount_symbols)) if strict else amount.fillna(0).sum(axis=1)
    result["amount_proxy"] = dollars
    result["amount_ma20"] = dollars.rolling(20).mean()
    result["amount_ratio"] = dollars.div(result.amount_ma20.replace(0, np.nan))
    if strict and not amount_verified:
        result[["amount_proxy", "amount_ma20", "amount_ratio"]] = np.nan
    r = result
    r["trend_points"] = ((q > ma20).astype(int) + (q > ma50).astype(int) + (ma20 > ma20.shift(5)).astype(int)) * 10
    r["acceleration_points"] = (r.rs5 > 0).astype(int) * 8 + (r.rs5 > r.rs20 / 4).astype(int) * 9 + (r.rs5 > r.previous_rs5).astype(int) * 8
    r["volume_points"] = np.select([(r.rs1 > 0) & (r.amount_ratio >= 1.3), (r.rs1 > 0) & (r.amount_ratio >= 1)], [20, 10], default=0)
    r["breadth_points"] = np.select([r.breadth >= 70, r.breadth >= 50], [15, 8], default=0)
    r["extension"] = (r.rs60 > 18) | (r.dist50 > 8)
    r["extension_points"] = (~r.extension).astype(int) * 10
    r["score"] = r[["trend_points", "acceleration_points", "volume_points", "breadth_points", "extension_points"]].sum(axis=1).where(r.valid)
    r["distribution"] = (q < ma20) & (r.rs5 < 0) & (r.amount_ratio > 1.3) & (r.rs1 < 0)
    r["source_state_code"] = r.apply(_source_state, axis=1)
    r["source_state"] = r.source_state_code.map(SOURCE_NAMES)
    r["source_trend"] = np.select(
        [(q > ma20) & (q > ma50), q > ma20,
         (q < ma20) & (q < ma50), q < ma20],
        ["强", "偏强", "弱", "偏弱"], default="中性")
    # Pine crossover/crossunder are strict on the current bar, inclusive on
    # the previous bar. These events are NOT state transitions or deliveries.
    for threshold in (60, 75):
        r[f"source_cross_up_{threshold}"] = (r.score > threshold) & (r.score.shift(1) <= threshold)
    r["source_cross_down_45"] = (r.score < 45) & (r.score.shift(1) >= 45)
    if strict:
        # Production score is experimental and requires all *real* inputs.
        sufficient = r.history_valid & r.breadth.notna() & (r.breadth_n >= 5) & r.amount_ratio.notna()
        sufficient &= r.breadth_n.ge(r.breadth_expected * .8)
        r["score"] = r.score.where(sufficient)
        add_states(r, schema_version=schema_version)
        r.drop(columns=[column for column in r.columns if column.startswith("source_")], inplace=True)
    return r


def add_states(frame: pd.DataFrame, confirmation: int = 2, *, schema_version: str | None = None):
    schema_version = schema_version or SCHEMA_VERSION
    if schema_version == LEGACY_SCHEMA_VERSION:
        add_states_v2(frame, confirmation=confirmation)
        return
    add_states_v3(frame, confirmation=confirmation)


def add_states_v2(frame: pd.DataFrame, confirmation: int = 2):
    """Replay complete daily bars, making repeated runs idempotent.

    +/-0.5% relative month and +/-0.2% log acceleration dead bands; no
    macro observation is reused as another independent price confirmation.
    """
    confirmed, pending, count, since = "unavailable", None, 0, None
    records = []
    for date, row in frame.iterrows():
        x = np.log1p(row.rs20 / 100)
        y = np.log1p(row.rs5 / 100) - (x - np.log1p(row.rs5 / 100)) / 3
        # Stable equality at the band edge, independent of log round-off.
        boundary = abs(x) <= .005 + 1e-12 or abs(y) <= .002 + 1e-12
        old = confirmed
        if not row.history_valid:
            confirmed, pending, count, since = "unavailable", None, 0, None
            candidate = "unavailable"
        elif boundary:
            candidate = "neutral"
            pending, count = None, 0
            if confirmed == "unavailable":
                confirmed = "pending"
        else:
            candidate = ("leading" if y > 0 else "cooling") if x > 0 else ("improving" if y > 0 else "lagging")
            if candidate == confirmed:
                pending, count = None, 0
            else:
                count = count + 1 if candidate == pending else 1
                pending = candidate
                if count >= confirmation:
                    confirmed, since, pending, count = candidate, date.date().isoformat(), None, 0
                elif confirmed == "unavailable":
                    confirmed = "pending"
        absolute_up = row.abs20 > 0 and row["index"] > row.absolute_ma20
        if not row.history_valid:
            action = "unavailable"
        elif row.extension:
            action = "extended"
        elif boundary or pending:
            action = "wait"
        elif confirmed == "leading" and absolute_up:
            breadth_ok = row.breadth_n >= 5 and row.breadth_n >= row.breadth_expected * .8
            action = "priority" if breadth_ok and row.breadth >= 60 else "price_watch"
        elif confirmed == "improving" and row.abs5 > 0:
            action = "watch"
        elif confirmed in {"cooling", "lagging"} or not absolute_up:
            action = "caution"
        else:
            action = "wait"
        records.append({"state": confirmed, "state_name": STATE_NAMES[confirmed],
                        "candidate": candidate, "confirmation_count": count,
                        "confirmation_required": confirmation, "state_since": since,
                        "state_clock": "completed_daily_bar", "last_evaluated_bar": date.date().isoformat(),
                        "state_since_scope": "replay_window", "exit_candidate": pending if old not in {"pending", "unavailable"} else None,
                        "transition": old != confirmed, "boundary": boundary,
                        "acceleration_log": y, "action": action})
    extra = pd.DataFrame(records, index=frame.index)
    for column in extra:
        frame[column] = extra[column]


def add_states_v3(frame: pd.DataFrame, confirmation: int = 2):
    confirmed, pending, count, since = "unavailable", None, 0, None
    records = []
    for date, row in frame.iterrows():
        x, y, strength, speed = classify_axes(row.rs5, row.rs20)
        # Strength dead-band only. Speed dead-band is "steady", not "cannot tell".
        boundary = np.isfinite(x) and abs(x) <= DX + 1e-12
        old = confirmed
        if not row.history_valid or strength == "unavailable":
            confirmed, pending, count, since = "unavailable", None, 0, None
            candidate = "unavailable"
            strength, speed = "unavailable", "unavailable"
        else:
            candidate = strength
            if candidate == confirmed:
                pending, count = None, 0
            else:
                count = count + 1 if candidate == pending else 1
                pending = candidate
                if count >= confirmation:
                    confirmed, since, pending, count = candidate, date.date().isoformat(), None, 0
        strength_for_priority = confirmed if confirmed in STRENGTH_LABELS else (
            candidate if candidate in STRENGTH_LABELS else "unavailable"
        )
        flags = []
        if bool(row.extension) if not pd.isna(row.extension) else False:
            flags.append("EXTENDED")
        if not pd.isna(row["index"]) and not pd.isna(row.absolute_ma20) and row["index"] <= row.absolute_ma20:
            flags.append("BELOW_ABSOLUTE_MA20")
        if not pd.isna(row.abs20) and row.abs20 <= 0:
            flags.append("ABSOLUTE_DOWNTREND")
        action = assign_priority(
            bool(row.history_valid), strength_for_priority, speed,
            row.abs20, row["index"], row.absolute_ma20, row.abs5,
        )
        combined = COMBINED_LABELS.get((strength, speed))
        if confirmed in STRENGTH_LABELS:
            state_name = COMBINED_LABELS.get((confirmed, speed), STRENGTH_LABELS[confirmed])
        else:
            state_name = combined or "数据不足"
        records.append({
            "strength_axis": strength, "speed_axis": speed, "speed_current": speed,
            "strength_confirmed": confirmed, "strength_candidate": candidate,
            "strength_confirmation_count": count, "strength_label": STRENGTH_LABELS.get(strength, "数据不足"),
            "speed_label": SPEED_LABELS.get(speed, "数据不足"),
            "combined_label": combined or "数据不足",
            "state": confirmed, "state_name": state_name,
            "candidate": candidate, "confirmation_count": count,
            "confirmation_required": confirmation, "state_since": since,
            "state_clock": "completed_daily_bar", "last_evaluated_bar": date.date().isoformat(),
            "state_since_scope": "replay_window",
            "exit_candidate": pending if old in STRENGTH_LABELS else None,
            "transition": old != confirmed, "boundary": bool(boundary),
            "strength_log": x if np.isfinite(x) else np.nan,
            "acceleration_log": y if np.isfinite(y) else np.nan,
            "priority": action, "action": action,
            "risk_flags": list(flags),
            "amount_label": amount_direction_label(row.rs1, row.abs1, row.amount_ratio),
        })
    extra = pd.DataFrame(records, index=frame.index)
    for column in extra:
        frame[column] = extra[column]


def member_records(theme, prices):
    result = []
    for symbol in theme.members:
        series = prices[symbol]
        ma = series.rolling(20).mean().iloc[-1]
        price = series.iloc[-1]
        result.append({"ticker": symbol, "security_id": symbol,
                       "close": price, "above_ma20": None if pd.isna(price) or pd.isna(ma) else bool(price > ma)})
    return result


def evidence_gaps_for(theme, latest, *, amount_verified):
    gaps = []
    if not latest["history_valid"]:
        gaps.append("INSUFFICIENT_HISTORY")
    if not theme.members:
        gaps.append("ETF_HOLDINGS_NOT_LINKED")
    elif len(theme.members) < 5:
        gaps.append("SMALL_BASKET")
    breadth = latest.get("breadth")
    n = latest.get("breadth_n") or 0
    if theme.members and breadth is not None and not (isinstance(breadth, float) and np.isnan(breadth)) and n >= 1:
        if breadth < 60:
            gaps.append("LOW_PARTICIPATION")
    if (latest.get("strength_confirmation_count") or 0) > 0:
        gaps.append("STATE_CONFIRMING")
    return gaps


def analyze(prices, volumes, sessions, themes, *, amount_verified=False,
            execution_close=None, schema_version=None):
    sessions = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
    if sessions.empty or sessions.has_duplicates or not sessions.is_monotonic_increasing:
        raise ValueError("Sessions must be unique, ordered and nonempty")
    schema_version = schema_version or SCHEMA_VERSION
    from .themes import required_symbols
    symbols = required_symbols(themes)
    prices = clean_table(prices, sessions).reindex(columns=symbols).where(lambda x: x > 0)
    volumes = clean_table(volumes, sessions).reindex(columns=symbols)
    if execution_close is None:
        exec_px = prices
    else:
        exec_px = clean_table(execution_close, sessions).reindex(columns=symbols).where(lambda x: x > 0)
    rows = []
    for theme in themes:
        compat = metric_frame(theme, prices, volumes, strict=False, execution_close=exec_px,
                              schema_version=schema_version)
        prod = metric_frame(theme, prices, volumes, strict=True, amount_verified=amount_verified,
                            execution_close=exec_px, schema_version=schema_version)
        latest = prod.iloc[-1].to_dict()
        if isinstance(latest.get("risk_flags"), tuple):
            latest["risk_flags"] = list(latest["risk_flags"])
        if schema_version != LEGACY_SCHEMA_VERSION:
            latest["evidence_gaps"] = evidence_gaps_for(theme, latest, amount_verified=amount_verified)
            latest["priority_name"] = PRIORITY_LABELS.get(
                latest.get("priority") or latest.get("action"), latest.get("action"))
        reasons = []
        if not latest["history_valid"]:
            reasons.append("不足61个连续有效收盘点，或最新行情缺失")
        if not theme.members:
            reasons.append("ETF真实持仓广度未接入，不能将趋势代理当成成员比例")
        elif len(theme.members) < 5:
            reasons.append("小样本篮子：仅作价格观察，广度不作为优先门槛")
        if not amount_verified:
            reasons.append("量价复权口径未核验，生产版量能与总分停用")
        if sessions[-1].date().isoformat() < max(theme.known_at, theme.effective_from or theme.known_at):
            reasons.append("观察日早于主题定义的已知/生效日期，仅供事后研究，不是PIT策略样本")
        history = prod.tail(120).reset_index(names="date")
        history["date"] = history.date.dt.strftime("%Y-%m-%d")
        if "risk_flags" in history.columns:
            history["risk_flags"] = history["risk_flags"].map(
                lambda value: list(value) if isinstance(value, (list, tuple)) else [])
        # Complete intermediate values remain available for rule-level replay.
        reference = compat.tail(120).reset_index(names="date")
        reference["date"] = reference.date.dt.strftime("%Y-%m-%d")
        row = {**theme.record(), "definition": theme.record(), "source_session": sessions[-1].date().isoformat(),
               "history_mode": "etf_native" if theme.proxy else "current_basket_backcast",
               "pit_membership": False,
               "breadth_kind": "member_above_ma" if theme.members else "unavailable",
               "index_method": "etf_price" if theme.proxy else "daily_equal_weight_segmented",
               "production": latest, "compatibility": compat.iloc[-1].to_dict(),
               "compatibility_scope": "public_v1" if theme.cohort == "technology" else "project_extension",
               "warnings": reasons, "members": member_records(theme, prices),
               "history": history.to_dict("records"), "reference_history": reference.to_dict("records")}
        if schema_version != LEGACY_SCHEMA_VERSION:
            row["evidence_gaps"] = latest["evidence_gaps"]
        rows.append(row)
    return rows
