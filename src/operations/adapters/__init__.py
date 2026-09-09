"""Read-only adapters that translate existing production evidence."""

from src.operations.adapters.application import collect_application_evidence
from src.operations.adapters.broad import collect_broad_evidence
from src.operations.adapters.delivery import collect_delivery_evidence
from src.operations.adapters.market import collect_market_evidence
from src.operations.adapters.research import collect_research_evidence

__all__ = [
    "collect_application_evidence",
    "collect_broad_evidence",
    "collect_delivery_evidence",
    "collect_market_evidence",
    "collect_research_evidence",
]
