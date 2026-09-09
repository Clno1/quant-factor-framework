"""
全局配置加载工具。

用法：
    from src.config import CONFIG
    print(CONFIG.research_universes.registry_path)  # 属性访问
    print(CONFIG["universe"]["name"])    # 字典访问
    CONFIG.reload()                      # 强制重载
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# 项目根目录：src/config.py 的上两层
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "configs" / "default.yaml"


class _AttrDict(dict):
    """支持属性访问的 dict（递归）。"""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc
        return _AttrDict(value) if isinstance(value, dict) else value

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value


class _ConfigProxy:
    """延迟加载的配置代理，支持 reload。"""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH):
        self._path = path
        self._data: _AttrDict | None = None

    def _ensure_loaded(self) -> None:
        if self._data is None:
            self.reload()

    def reload(self, path: Path | None = None) -> None:
        if path is not None:
            self._path = path
        if not self._path.exists():
            raise FileNotFoundError(f"Config file not found: {self._path}")
        with self._path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        self._data = _AttrDict(raw)

    # 属性访问代理
    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        self._ensure_loaded()
        return getattr(self._data, key)

    # 字典访问代理
    def __getitem__(self, key: str) -> Any:
        self._ensure_loaded()
        return self._data[key]

    def to_dict(self) -> dict:
        self._ensure_loaded()
        return dict(self._data)  # type: ignore[arg-type]

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    def abs_path(self, relative: str) -> Path:
        """将配置中的相对路径转为绝对路径。"""
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p


# 全局唯一实例
CONFIG = _ConfigProxy()


__all__ = ["CONFIG", "PROJECT_ROOT"]
