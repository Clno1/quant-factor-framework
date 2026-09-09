"""Watchlist 数据结构与校验。"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import math
from typing import Any, Iterable

from src.utils.identifiers import canonical_ticker


@dataclass
class WatchlistItem:
    """Watchlist 的一行：股票 + 公司名（可选） + 权重。"""
    ticker: str
    weight: float = 0.0
    name: str = ""          # 公司名，来源于 FMP profile，展示用

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "weight": float(self.weight),
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WatchlistItem":
        return cls(
            ticker=str(d["ticker"]).strip().upper(),
            weight=float(d.get("weight") or 0.0),
            name=str(d.get("name") or ""),
        )


@dataclass
class WatchlistDefinition:
    """Watchlist 定义。id 是 UUID，name 可以重复（按 id 唯一）。"""
    id: str
    name: str
    description: str = ""
    items: list[WatchlistItem] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    schema_version: int = 1

    # --------- 构造 ---------

    @classmethod
    def new(
        cls,
        name: str,
        description: str = "",
        items: Iterable[WatchlistItem] | None = None,
    ) -> "WatchlistDefinition":
        now = datetime.now().isoformat(timespec="seconds")
        return cls(
            id=str(uuid.uuid4()),
            name=str(name).strip() or "未命名股票组",
            description=str(description or "").strip(),
            items=list(items or []),
            created_at=now,
            updated_at=now,
        )

    # --------- 序列化 ---------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "items": [it.to_dict() for it in self.items],
            "universe_type": "TARGET",
            "ticker_revision_sha256": self.ticker_revision_sha256(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WatchlistDefinition":
        items = [WatchlistItem.from_dict(it) for it in (d.get("items") or [])]
        return cls(
            id=str(d["id"]),
            name=str(d.get("name") or "未命名股票组"),
            description=str(d.get("description") or ""),
            items=items,
            created_at=str(d.get("created_at") or ""),
            updated_at=str(d.get("updated_at") or ""),
            schema_version=int(d.get("schema_version") or 1),
        )

    # --------- 校验 & 清洗 ---------

    def validate(self) -> None:
        """基础校验：至少 1 个 ticker、权重非负、ticker 不重复。"""
        if not self.name:
            raise ValueError("Watchlist 名称不能为空")
        if not self.items:
            raise ValueError("Watchlist 至少需要包含 1 个股票")
        seen: set[str] = set()
        for it in self.items:
            it.ticker = canonical_ticker(it.ticker)
            if it.ticker in seen:
                raise ValueError(f"ticker 重复：{it.ticker}")
            seen.add(it.ticker)
            if not math.isfinite(float(it.weight)):
                raise ValueError(f"权重必须是有限数字：{it.ticker}")
            if it.weight < 0:
                raise ValueError(f"权重不能为负：{it.ticker} -> {it.weight}")

    def tickers(self) -> list[str]:
        return [it.ticker for it in self.items]

    def ticker_revision_sha256(self) -> str:
        """Hash the canonical ticker set that defines this target-pool revision."""
        normalized = sorted(
            {
                str(item.ticker).strip().upper()
                for item in self.items
                if str(item.ticker).strip()
            }
        )
        digest = hashlib.sha256(",".join(normalized).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def set_equal_weights(self) -> None:
        """一键等权。"""
        n = len(self.items)
        if n == 0:
            return
        w = 1.0 / n
        for it in self.items:
            it.weight = w

    def normalize_weights(self) -> None:
        """把权重归一化到和为 1（保持原始比例）。全 0 时退化为等权。"""
        total = sum(it.weight for it in self.items)
        if total <= 0:
            self.set_equal_weights()
            return
        for it in self.items:
            it.weight = it.weight / total


def normalize_weights(items: Iterable[WatchlistItem]) -> list[WatchlistItem]:
    """独立的函数版本：返回新列表，不修改入参。"""
    items = list(items)
    total = sum(it.weight for it in items)
    if total <= 0:
        n = len(items) or 1
        return [WatchlistItem(it.ticker, 1.0 / n, it.name) for it in items]
    return [
        WatchlistItem(it.ticker, it.weight / total, it.name) for it in items
    ]
