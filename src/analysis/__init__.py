"""单因子有效性检验。"""
import src.analysis.confidence as _confidence

from src.analysis.confidence_hac import confidence_ic_stats_hac

# Compatibility bridge while confidence.py keeps its public API stable.  All
# confidence entry points resolve the module-global _ic_stats at runtime, so
# replacing that one internal estimator upgrades formal t/p/q gates to HAC
# without duplicating the large report-building implementation.
_confidence._ic_stats = confidence_ic_stats_hac

from src.analysis.confidence import (  # noqa: E402
    ConfidenceArtifacts,
    build_factor_confidence,
    compute_quantile_turnover,
    compute_rank_autocorrelation,
    finalize_confidence_reports,
)
from src.analysis.ic import (  # noqa: E402
    compute_forward_returns,
    compute_ic,
    ic_summary,
    ic_summary_table,
)

__all__ = [
    "ConfidenceArtifacts",
    "build_factor_confidence",
    "compute_quantile_turnover",
    "compute_rank_autocorrelation",
    "finalize_confidence_reports",
    "compute_forward_returns",
    "compute_ic",
    "ic_summary",
    "ic_summary_table",
]
