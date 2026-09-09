"""
策略定义数据类。

策略 = 名称 + 描述 + 若干因子 + 权重。纯配方，不绑定股票池，不含回测结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
import math
from typing import Any
from uuid import uuid4

from src.factors import assert_valid_factor_ids


class StrategyValidationError(ValueError):
    """策略定义校验失败（缺因子、权重非法等）。"""


@dataclass
class StrategyComponent:
    factor_id: str
    weight: float

    def to_dict(self) -> dict[str, Any]:
        return {"factor_id": self.factor_id, "weight": float(self.weight)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StrategyComponent":
        return cls(factor_id=str(d["factor_id"]), weight=float(d["weight"]))


@dataclass
class StrategyDefinition:
    id: str
    name: str
    description: str
    components: list[StrategyComponent]
    created_at: str
    schema_version: int = 1

    # ---------- 工厂 ----------

    @classmethod
    def new(
        cls,
        name: str,
        description: str,
        components: list[StrategyComponent],
    ) -> "StrategyDefinition":
        return cls(
            id=str(uuid4()),
            name=name.strip(),
            description=(description or "").strip(),
            components=components,
            created_at=datetime.now().isoformat(timespec="seconds"),
            schema_version=1,
        )

    # ---------- 校验 ----------

    def validate(self) -> None:
        if not self.name:
            raise StrategyValidationError("策略名称不能为空")
        if len(self.name) > 100:
            raise StrategyValidationError("策略名称过长（>100 字符）")
        if len(self.description) > 500:
            raise StrategyValidationError("策略描述过长（>500 字符）")
        if not self.components:
            raise StrategyValidationError("策略至少要包含 1 个因子")
        if len(self.components) > 32:
            raise StrategyValidationError("单个策略因子数不应超过 32")

        # 因子不能重复
        ids = [c.factor_id for c in self.components]
        if len(set(ids)) != len(ids):
            raise StrategyValidationError(f"因子重复：{ids}")

        # 因子必须已注册
        assert_valid_factor_ids(ids)  # 抛 FactorLibraryError

        # 权重合法性：非零且不全零
        for c in self.components:
            if c.weight is None:
                raise StrategyValidationError(f"因子 {c.factor_id} 权重不能为空")
            if not isinstance(c.weight, (int, float)):
                raise StrategyValidationError(f"因子 {c.factor_id} 权重必须为数字")
            if not math.isfinite(float(c.weight)):
                raise StrategyValidationError(
                    f"因子 {c.factor_id} 权重必须是有限数字"
                )
            if abs(float(c.weight)) <= 0.0:
                raise StrategyValidationError(
                    f"因子 {c.factor_id} 权重不能为 0；不使用的因子应删除"
                )
        total_abs = sum(abs(c.weight) for c in self.components)
        if total_abs <= 0:
            raise StrategyValidationError("权重不能全部为 0")

    # ---------- 归一化 ----------

    def normalized(self) -> "StrategyDefinition":
        """
        返回权重按 |w|/Σ|w| 归一化后的副本，保留原正负号。
        合成用此归一化值；展示仍可用原始值。
        """
        total = sum(abs(c.weight) for c in self.components)
        if total <= 0:
            return self
        new_components = [
            StrategyComponent(factor_id=c.factor_id, weight=c.weight / total)
            for c in self.components
        ]
        return StrategyDefinition(
            id=self.id,
            name=self.name,
            description=self.description,
            components=new_components,
            created_at=self.created_at,
            schema_version=self.schema_version,
        )

    # ---------- 序列化 ----------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "components": [c.to_dict() for c in self.components],
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StrategyDefinition":
        return cls(
            id=str(d["id"]),
            name=str(d.get("name", "")),
            description=str(d.get("description", "")),
            components=[StrategyComponent.from_dict(c) for c in d.get("components", [])],
            created_at=str(d.get("created_at", "")),
            schema_version=int(d.get("schema_version", 1)),
        )


__all__ = [
    "StrategyComponent",
    "StrategyDefinition",
    "StrategyValidationError",
]
