"""Neutral access to persisted factor matrices used outside the Web layer."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.config import CONFIG, PROJECT_ROOT
from src.utils.io import read_parquet


DEFAULT_UNIVERSE = "SP500"


def factor_values_path(name: str, universe: str = DEFAULT_UNIVERSE) -> Path:
    configured = Path(CONFIG.webapp.output_dir)
    output_dir = configured if configured.is_absolute() else PROJECT_ROOT / configured
    return output_dir / "universes" / universe / "factors" / name / "factor_values.parquet"


def load_factor_values(
    name: str,
    universe: str = DEFAULT_UNIVERSE,
) -> pd.DataFrame | None:
    """Load a persisted factor matrix without depending on FastAPI modules."""
    path = factor_values_path(name, universe)
    return read_parquet(path) if path.exists() else None


__all__ = ["DEFAULT_UNIVERSE", "factor_values_path", "load_factor_values"]

