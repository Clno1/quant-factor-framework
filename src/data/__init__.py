"""Published market-data access and universe discovery."""
from src.data.universe import get_universe
from src.data.cleaner import build_wide_tables, load_wide_tables
from src.data.pit import apply_point_in_time_mask, find_membership_file
from src.data.integrity import install_data_integrity_adapter

# Upgrade every MarketDataReader wide-table consumer at the shared boundary.
install_data_integrity_adapter()

__all__ = [
    "get_universe",
    "build_wide_tables",
    "load_wide_tables",
    "apply_point_in_time_mask",
    "find_membership_file",
]
