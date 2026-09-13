"""ETF net creation (Δ split-adjusted shares × NAV). Never a turnover substitute."""
from __future__ import annotations

from pathlib import Path
import hashlib
import math

import pandas as pd

from src.config import PROJECT_ROOT

from . import FLOWS_AUDIT_DOC, FLOWS_AUDIT_STATUS
from .store import encoded

FLOW_LABELS = {
    ("up", "in"): "价格涨+净流入",
    ("up", "out"): "价格涨+净流出",
    ("down", "in"): "价格跌+净流入",
    ("down", "out"): "价格跌+净流出",
}
UNAVAILABLE_NOTE = "FMP未提供可审计的ETF历史份额/NAV序列；不用成交额冒充净申赎"
BASKET_NOTE = "自建篮子没有ETF份额，净申赎不适用"


def default_flows_root():
    return PROJECT_ROOT / "data" / "reference" / "group_analytics" / "rotation" / "flows"


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def compute_net_creation(shares, nav):
    """Daily net creation ≈ Δ split-adjusted shares × NAV.

    ``shares`` and ``nav`` must share a date index. This is arithmetic only;
    it does not fetch a vendor series.
    """
    share_s = pd.to_numeric(shares, errors="coerce")
    nav_s = pd.to_numeric(nav, errors="coerce")
    aligned = pd.DataFrame({"shares": share_s, "nav": nav_s}).sort_index()
    aligned = aligned.loc[~aligned.index.duplicated(keep="last")]
    delta = aligned["shares"] - aligned["shares"].shift(1)
    daily = delta * aligned["nav"]
    return pd.DataFrame({
        "shares": aligned["shares"],
        "nav": aligned["nav"],
        "delta_shares": delta,
        "net_creation": daily,
        "net_creation_5": daily.rolling(5, min_periods=5).sum(),
        "net_creation_20": daily.rolling(20, min_periods=20).sum(),
    }, index=aligned.index)


def flow_label(abs1, net_creation):
    price = _finite(abs1)
    flow = _finite(net_creation)
    if price is None or flow is None or price == 0 or flow == 0:
        return None
    side = ("up", "in") if price > 0 else ("down", "in")
    if flow < 0:
        side = (side[0], "out")
    return FLOW_LABELS[side]


def _unavailable(row):
    return {
        "status": "unavailable",
        "audit_status": FLOWS_AUDIT_STATUS,
        "audit_doc": FLOWS_AUDIT_DOC,
        "asof": None,
        "daily": None,
        "cumulative_5": None,
        "cumulative_20": None,
        "label": None,
        "note": UNAVAILABLE_NOTE,
        "amount_proxy": None,
    }


def _not_applicable():
    return {
        "status": "not_applicable",
        "kind": "custom_basket",
        "audit_status": FLOWS_AUDIT_STATUS,
        "audit_doc": FLOWS_AUDIT_DOC,
        "asof": None,
        "daily": None,
        "cumulative_5": None,
        "cumulative_20": None,
        "label": None,
        "note": BASKET_NOTE,
        "amount_proxy": None,
    }


def attach_net_creation(rows, series_by_proxy=None, *, audit_status=None):
    """Attach descriptive net-creation fields. Baskets are always not applicable.

    Live vendor series are refused while the flows audit is NOT_PASSED, even if
    a caller passes frames. Tests may pass audit_status='PASSED' with synthetic
    series. Amount/turnover is never copied into this object.
    """
    series_by_proxy = series_by_proxy or {}
    status = FLOWS_AUDIT_STATUS if audit_status is None else audit_status
    for row in rows:
        has_basket_members = bool(row.get("definition", {}).get("members"))
        if has_basket_members or not row.get("proxy"):
            row["net_creation"] = _not_applicable()
            continue
        if status != "PASSED":
            row["net_creation"] = _unavailable(row)
            continue
        frame = series_by_proxy.get(row["proxy"])
        if frame is None or getattr(frame, "empty", True):
            row["net_creation"] = _unavailable(row)
            continue
        latest = frame.iloc[-1]
        daily = _finite(latest.get("net_creation") if hasattr(latest, "get") else latest["net_creation"])
        abs1 = (row.get("production") or {}).get("abs1")
        row["net_creation"] = {
            "status": "available" if daily is not None else "unavailable",
            "audit_status": status,
            "audit_doc": FLOWS_AUDIT_DOC,
            "asof": (frame.index[-1].date().isoformat()
                     if hasattr(frame.index[-1], "date") else str(frame.index[-1])),
            "daily": daily,
            "cumulative_5": _finite(latest["net_creation_5"] if "net_creation_5" in frame.columns else None),
            "cumulative_20": _finite(latest["net_creation_20"] if "net_creation_20" in frame.columns else None),
            "label": flow_label(abs1, daily),
            "note": "日净申赎 ≈ 拆股调整后份额变化 × 当日NAV；描述标签不进优先级",
            "amount_proxy": None,
        }
    return rows


def flows_fingerprint(rows):
    payload = {
        row.get("id"): {
            "status": (row.get("net_creation") or {}).get("status"),
            "asof": (row.get("net_creation") or {}).get("asof"),
            "daily": (row.get("net_creation") or {}).get("daily"),
        }
        for row in rows
    }
    return hashlib.sha256(encoded(payload)).hexdigest() if payload else None


def load_flow_frames(root, symbols):
    """Optional on-disk series. Unused until the flows audit passes."""
    base = Path(root) if root else default_flows_root()
    frames = {}
    if not base.is_dir():
        return frames
    for symbol in symbols:
        path = base / f"{symbol}.parquet"
        if path.is_file() and not path.is_symlink():
            frames[symbol] = pd.read_parquet(path)
    return frames
