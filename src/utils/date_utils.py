"""
灵活的日期字符串解析，支持配置里的"动态日期"写法。

支持的输入格式：
    - "today"          → 今天
    - "yesterday"      → 昨天
    - "5Y" / "5y"      → 5 年前
    - "3M" / "3m"      → 3 个月前
    - "90D" / "90d"    → 90 天前
    - "2020-01-01"     → 标准 ISO 日期
    - "2020-01-01" 作为 end，也兼容未来日期（不会越过 today）

所有结果：
    - parse_date(x) 返回 datetime.date
    - parse_date_str(x) 返回 "YYYY-MM-DD" 字符串
    - is_dynamic(x)     返回 True 表示 x 是"动态"（today / nY / nM 等）
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Union

import pandas as pd


_RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([dDmMyY])\s*$")


def _today() -> date:
    return date.today()


def is_dynamic(value: Union[str, None]) -> bool:
    """判断配置值是否是"动态"（每次调用可能返回不同日期）。"""
    if value is None:
        return False
    s = str(value).strip().lower()
    if s in ("today", "yesterday", "now"):
        return True
    return bool(_RELATIVE_RE.match(s))


def parse_date(value: Union[str, None], *, default: Union[date, None] = None) -> date:
    """把配置字符串解析成 date 对象。"""
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise ValueError("empty date value")

    s = str(value).strip().lower()

    if s in ("today", "now"):
        return _today()
    if s == "yesterday":
        return _today() - timedelta(days=1)

    m = _RELATIVE_RE.match(s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        today = _today()
        if unit == "d":
            return today - timedelta(days=n)
        if unit == "m":
            # 简化处理：n 个月 = n * 30 天（对量化研究够用；要精确就 pd.DateOffset）
            return (pd.Timestamp(today) - pd.DateOffset(months=n)).date()
        if unit == "y":
            return (pd.Timestamp(today) - pd.DateOffset(years=n)).date()

    # ISO 日期
    try:
        return pd.Timestamp(value).date()
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"Cannot parse date: {value!r} ({e})") from e


def parse_date_str(value: Union[str, None], *, default: Union[date, None] = None) -> str:
    """解析后返回 "YYYY-MM-DD" 字符串，供 FMP / yfinance 等 API 使用。"""
    return parse_date(value, default=default).isoformat()


def resolve_date_range(
    start: Union[str, None],
    end: Union[str, None],
) -> tuple[str, str, bool]:
    """
    解析一对 start/end 字符串。

    返回 (start_iso, end_iso, has_dynamic)
    - has_dynamic: 只要 start 或 end 有一个是动态的就 True，调用方可据此决定是否强制刷新数据
    """
    end_d   = parse_date(end)
    start_d = parse_date(start)
    # 防御：start 不能晚于 end
    if start_d > end_d:
        raise ValueError(f"start ({start_d}) > end ({end_d})")
    return start_d.isoformat(), end_d.isoformat(), (is_dynamic(start) or is_dynamic(end))


__all__ = [
    "parse_date",
    "parse_date_str",
    "resolve_date_range",
    "is_dynamic",
]
