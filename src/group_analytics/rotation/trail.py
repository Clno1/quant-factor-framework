"""Compact strength-speed trail for the cohort scatter.

Explanation only. Never writes production, never changes assign_priority.
Not a JdK RS-Ratio / RS-Momentum replica.
"""
from __future__ import annotations

import math

TRAIL_VERSION = "strength-speed-trail-v1"
TRAIL_BARS = 50  # ~10 equity trading weeks
TRAIL_NOTE = "借鉴相对趋势与轨迹表达，非RRG复刻"


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def attach_rotation_trail(rows, *, bars=TRAIL_BARS):
    """Replace row['rotation_trail'] from production history. Idempotent."""
    for row in rows:
        history = row.get("history") or []
        window = history[-bars:]
        points = []
        for bar in window:
            x = _finite(bar.get("strength_log"))
            y = _finite(bar.get("acceleration_log"))
            ok = bool(bar.get("history_valid")) and x is not None and y is not None
            points.append({
                "date": bar.get("date"),
                "strength_log": x if ok else None,
                "acceleration_log": y if ok else None,
            })
        production = row.get("production") or {}
        row["rotation_trail"] = {
            "version": TRAIL_VERSION,
            "window_bars": bars,
            "window_label": "最近10周",
            "points": points,
            "current": {
                "date": None if not history else history[-1].get("date"),
                "strength_log": _finite(production.get("strength_log")),
                "acceleration_log": _finite(production.get("acceleration_log")),
            },
            "note": TRAIL_NOTE,
        }
    return rows
