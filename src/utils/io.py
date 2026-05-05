"""
Parquet / JSON IO 工具，带目录自动创建与缓存有效期校验。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Union

import pandas as pd

PathLike = Union[str, Path]


def ensure_dir(path: PathLike) -> Path:
    """确保目录存在（若传入文件路径，则创建其父目录），返回 Path。"""
    p = Path(path)
    target = p if (p.suffix == "" and not p.is_file()) else p.parent
    target.mkdir(parents=True, exist_ok=True)
    return p


def is_cache_fresh(path: PathLike, max_age_days: float) -> bool:
    """文件是否存在且未过期。"""
    p = Path(path)
    if not p.exists():
        return False
    if max_age_days <= 0:
        return True  # 负数 / 0 视为永不过期
    age_seconds = time.time() - p.stat().st_mtime
    return age_seconds <= max_age_days * 86400.0


def write_parquet(df: pd.DataFrame, path: PathLike, compression: str = "snappy") -> Path:
    p = Path(path)
    ensure_dir(p)
    df.to_parquet(p, compression=compression)
    return p


def read_parquet(path: PathLike) -> pd.DataFrame:
    return pd.read_parquet(Path(path))


def save_json(obj: Any, path: PathLike, indent: int = 2) -> Path:
    p = Path(path)
    ensure_dir(p)
    with p.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent, default=str)
    return p


def load_json(path: PathLike) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


__all__ = [
    "ensure_dir",
    "is_cache_fresh",
    "write_parquet",
    "read_parquet",
    "save_json",
    "load_json",
]
