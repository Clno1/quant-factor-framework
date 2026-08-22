from __future__ import annotations

import numpy as np
import pandas as pd

import src.analysis.confidence as confidence_module
from src.analysis.confidence_hac import confidence_ic_stats_hac


def test_confidence_module_is_routed_to_hac_estimator() -> None:
    # Importing the src.analysis package installs the formal inference adapter.
    import src.analysis  # noqa: F401

    assert confidence_module._ic_stats is confidence_ic_stats_hac


def test_confidence_hac_uses_forward_horizon_minus_one_lags() -> None:
    rng = np.random.default_rng(19)
    values = np.zeros(300)
    shocks = rng.normal(0.0, 0.02, size=len(values))
    for i in range(1, len(values)):
        values[i] = 0.7 * values[i - 1] + shocks[i] + 0.001
    ic = pd.Series(values, index=pd.bdate_range("2024-01-02", periods=len(values)))
    ic.attrs["forward_periods"] = 5

    stats = confidence_ic_stats_hac(ic, direction_sign=1)
    assert stats["hac_lags"] == 4
    assert np.isfinite(stats["t_stat"])
    assert np.isfinite(stats["p_value"])
    assert np.isfinite(stats["ci95_low"])
    assert np.isfinite(stats["ci95_high"])
