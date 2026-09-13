"""Same-cohort rank path, persistence, and breadth change.

Explanation fields only. Never writes production, never changes assign_priority.
Ranks stay inside one (cohort, benchmark) group: technology vs QQQ is never
mixed with sectors vs SPY.
"""
from __future__ import annotations

from collections import defaultdict
import math

TIMELINE_VERSION = "cohort-rank-v1"
RANK_PATH_OFFSETS = (20, 15, 10, 5, 0)
RANK_LOOKBACK = 20
OUTPERFORM_WINDOW = 20
SPIKE_LOG_SHARE = 0.5
PERSISTENCE_LABELS = {
    "gradual": "逐步跑赢",
    "event_spike": "近期少数日贡献为主",
    "repair": "反弹未收复",
    "fading": "20日仍领先但近端掉队",
    "lagging": "近20日落后",
    "unavailable": "不足",
}
ETF_NO_PIT_NOTE = "持仓观测无历史时点，不能比较20日前广度"
COHORT_NOTE = "同组同基准排名，不跨科技/行业混排"
RANK_N_NOTE = "20日前与今日的可排名主题数不同，位次变化仅供参考"


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _history_bar(history, offset):
    if not history or offset < 0 or offset >= len(history):
        return None
    return history[-1 - offset]


def _rankable(bar):
    return bool(bar) and bool(bar.get("history_valid")) and _finite(bar.get("rs20")) is not None


def _log_share(rs5, rs20):
    if rs5 is None or rs20 is None:
        return None
    try:
        numerator = math.log1p(rs5 / 100.0)
        denominator = math.log1p(rs20 / 100.0)
    except ValueError:
        return None
    if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator == 0:
        return None
    return numerator / denominator


def classify_persistence(history_valid, rs5, rs20, share):
    if not history_valid or rs20 is None:
        return "unavailable"
    if rs20 > 0:
        if rs5 is not None and rs5 < 0:
            return "fading"
        if share is not None and share >= SPIKE_LOG_SHARE:
            return "event_spike"
        return "gradual"
    if rs5 is not None and rs5 > 0:
        return "repair"
    return "lagging"


def _ranks_at(rows, offset):
    eligible = []
    for row in rows:
        bar = _history_bar(row.get("history") or [], offset)
        if _rankable(bar):
            eligible.append((row["id"], _finite(bar["rs20"])))
    eligible.sort(key=lambda item: (-item[1], item[0]))
    size = len(eligible)
    return {theme_id: (index + 1, size, rs20) for index, (theme_id, rs20) in enumerate(eligible)}


def _outperform_days(history):
    if not history or len(history) < OUTPERFORM_WINDOW:
        return None
    values = [_finite(bar.get("rs1")) for bar in history[-OUTPERFORM_WINDOW:]]
    if any(value is None for value in values):
        return None
    return sum(1 for value in values if value > 0)


def _empty_timeline(*, cohort, benchmark, notes=()):
    return {
        "version": TIMELINE_VERSION,
        "cohort": cohort,
        "benchmark": benchmark,
        "rank_rs20": None,
        "rank_rs20_n": None,
        "rank_rs20_ago20": None,
        "rank_change20": None,
        "rank_path20": [{"offset": offset, "date": None, "rank": None, "n": None, "rs20": None}
                        for offset in RANK_PATH_OFFSETS],
        "outperform_days20": None,
        "log_share_5_of_20": None,
        "persistence": "unavailable",
        "persistence_label": PERSISTENCE_LABELS["unavailable"],
        "breadth_now": None,
        "breadth_ago20": None,
        "breadth_change20": None,
        "breadth_source": "unavailable",
        "notes": list(notes),
    }


def _timeline_for(row, ranks_by_offset):
    cohort = row.get("cohort")
    benchmark = row.get("benchmark")
    history = row.get("history") or []
    production = row.get("production") or {}
    history_valid = bool(production.get("history_valid"))
    latest = _history_bar(history, 0)
    ago = _history_bar(history, RANK_LOOKBACK)
    rs5 = _finite(production.get("rs5"))
    rs20 = _finite(production.get("rs20"))
    share = _log_share(rs5, rs20)
    persistence = classify_persistence(history_valid, rs5, rs20, share)
    notes = [COHORT_NOTE]
    now_rank = ranks_by_offset[0].get(row["id"])
    ago_rank = ranks_by_offset[RANK_LOOKBACK].get(row["id"])
    rank_now, n_now = (now_rank[0], now_rank[1]) if now_rank else (None, None)
    rank_ago, n_ago = (ago_rank[0], ago_rank[1]) if ago_rank else (None, None)
    change = None if rank_now is None or rank_ago is None else rank_ago - rank_now
    if rank_now is not None and rank_ago is not None and n_now != n_ago:
        notes.append(RANK_N_NOTE)
    path = []
    for offset in RANK_PATH_OFFSETS:
        bar = _history_bar(history, offset)
        ranked = ranks_by_offset[offset].get(row["id"])
        path.append({
            "offset": offset,
            "date": None if bar is None else bar.get("date"),
            "rank": None if ranked is None else ranked[0],
            "n": None if not ranks_by_offset[offset] else len(ranks_by_offset[offset]),
            "rs20": None if ranked is None else ranked[2],
        })
    breadth_now = _finite((latest or {}).get("breadth") if latest is not None else production.get("breadth"))
    breadth_ago = _finite((ago or {}).get("breadth")) if ago is not None else None
    if breadth_now is None:
        breadth_now = _finite(production.get("breadth"))
    change_breadth = None if breadth_now is None or breadth_ago is None else breadth_now - breadth_ago
    source = "production_history" if breadth_now is not None or breadth_ago is not None else "unavailable"
    return {
        "version": TIMELINE_VERSION,
        "cohort": cohort,
        "benchmark": benchmark,
        "rank_rs20": rank_now,
        "rank_rs20_n": n_now,
        "rank_rs20_ago20": rank_ago,
        "rank_change20": change,
        "rank_path20": path,
        "outperform_days20": _outperform_days(history),
        "log_share_5_of_20": share,
        "persistence": persistence,
        "persistence_label": PERSISTENCE_LABELS[persistence],
        "breadth_now": breadth_now,
        "breadth_ago20": breadth_ago,
        "breadth_change20": change_breadth,
        "breadth_source": source,
        "notes": notes,
    }


def attach_rotation_timeline(rows):
    """Replace row['rotation_timeline'] from production history. Idempotent."""
    groups = defaultdict(list)
    for row in rows:
        groups[(row.get("cohort"), row.get("benchmark"))].append(row)
    for group in groups.values():
        ranks_by_offset = {offset: _ranks_at(group, offset) for offset in RANK_PATH_OFFSETS}
        for row in group:
            if not (row.get("history") or []) or not row.get("production"):
                row["rotation_timeline"] = _empty_timeline(
                    cohort=row.get("cohort"), benchmark=row.get("benchmark"), notes=[COHORT_NOTE],
                )
            else:
                row["rotation_timeline"] = _timeline_for(row, ranks_by_offset)
    return rows


def refresh_timeline_breadth(rows):
    """After holdings overlay: ETF current breadth only; never invent PIT history."""
    for row in rows:
        timeline = row.get("rotation_timeline")
        if not isinstance(timeline, dict):
            continue
        overlay = row.get("holdings_breadth") or {}
        if overlay.get("breadth_kind") != "etf_holdings_observation":
            continue
        timeline["breadth_now"] = _finite(overlay.get("breadth_equal_weight_pct"))
        timeline["breadth_ago20"] = None
        timeline["breadth_change20"] = None
        timeline["breadth_source"] = "etf_holdings_current_only"
        notes = [item for item in (timeline.get("notes") or []) if item != ETF_NO_PIT_NOTE]
        notes.append(ETF_NO_PIT_NOTE)
        timeline["notes"] = notes
    return rows


def format_rotation_timeline_text(timeline):
    if not isinstance(timeline, dict):
        return None
    parts = []
    rank = timeline.get("rank_rs20")
    size = timeline.get("rank_rs20_n")
    if rank is not None and size:
        parts.append(f"同组第{int(rank)}/{int(size)}")
    ago = timeline.get("rank_rs20_ago20")
    if ago is not None:
        parts.append(f"20日前第{int(ago)}")
    persistence = timeline.get("persistence")
    label = timeline.get("persistence_label")
    if persistence not in {None, "unavailable"} and label:
        parts.append(label)
    return " · ".join(parts) if parts else None
