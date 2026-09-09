from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from src.preprocessing.pipeline import preprocess_factor


def _config():
    return SimpleNamespace(
        winsorize_method="mad",
        winsorize_n=3,
        neutralize_industry=True,
        neutralize_mcap=False,
        neutralize_min_obs=3,
        standardize=True,
    )


def test_unknown_industry_is_audited_but_not_silently_removed():
    index = pd.to_datetime(["2026-07-20", "2026-07-21"])
    raw = pd.DataFrame(
        [[1.0, 2.0, 3.0, 4.0], [1.5, 2.5, 3.5, 4.5]],
        index=index,
        columns=["AAA", "BBB", "CCC", "OLD"],
    )
    sector = pd.DataFrame(
        {"sector": ["Tech", "Finance", "Tech", "UNKNOWN"]},
        index=raw.columns,
    )

    with patch("src.preprocessing.pipeline.CONFIG.preprocessing", _config()), patch(
        "src.preprocessing.neutralize.CONFIG.preprocessing", _config()
    ):
        clean, audit = preprocess_factor(
            raw,
            sector_map=sector,
            membership_mask=raw.notna(),
            return_audit=True,
        )

    assert clean["OLD"].notna().all()
    assert audit.raw_non_null_clean_all_null_tickers == ()
    assert audit.neutralization["missing_industry_observations"] == 2
    assert audit.neutralization["industry_coverage"] == 0.75
    # Static/latest-known sector labels remain useful for coverage diagnostics,
    # but formal historical residualization must not use them as regressors.
    assert audit.neutralization["enabled_industry"] is False
    assert not any(row["applied"] for row in audit.neutralization["daily"])
    assert "non_pit" in str(audit.neutralization["industry_skip_reason"])
