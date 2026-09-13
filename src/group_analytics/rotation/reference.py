"""Read-only identity/classification adapter; all writes remain in rotation."""
import pandas as pd

from src.config import CONFIG
from src.data.foundation import DataFoundationError, MarketDataReader
from src.data.security_master_store import SecurityMasterStore
from src.data.universe_ids import US_EQUITY_COVERAGE


def bound_reference(reader=None, version=None):
    reader = reader or MarketDataReader()
    version = version or reader.require_latest(US_EQUITY_COVERAGE, require_price_semantics=True)
    manifest = reader.verify_version(version, require_price_semantics=True, verify_partition_children=False)
    store = SecurityMasterStore(reader.catalog.path, CONFIG.abs_path(str(CONFIG.data.security_master.snapshot_dir)))
    identity = manifest.get("security_master_generation_id")
    if not identity:
        raise DataFoundationError("Missing bound Security Master")
    generation, frames = store.load_generation(identity)
    if generation.manifest_sha256 != manifest.get("security_master_manifest_sha256"):
        raise DataFoundationError("Security Master binding mismatch")
    return version, frames, generation


def equity_lookup(master):
    selected = master.loc[
        master.asset_type.isin(["STOCK", "ADR"])
        & master.primary_exchange.isin(["NASDAQ", "NYSE", "AMEX"])
        & master.trading_status.eq("ACTIVE")
    ]
    if selected.current_ticker.duplicated().any():
        raise DataFoundationError("Ambiguous current equity ticker")
    return selected.set_index("current_ticker").to_dict("index")


def current_members(frame):
    result = frame.copy()
    for field in ("is_current_coverage", "is_current_member"):
        if field in result:
            result = result.loc[result[field].fillna(False).eq(True)]
    return result


def classified_members(members, frames, source_session):
    result = current_members(members)
    classes = frames["classifications"].copy()
    target = pd.Timestamp(source_session).normalize()
    for key in ("effective_from", "effective_to", "knowledge_date"):
        classes[key] = pd.to_datetime(classes[key], errors="coerce").dt.normalize()
    classes = classes.loc[
        (classes.effective_from.isna() | classes.effective_from.le(target))
        & (classes.effective_to.isna() | classes.effective_to.ge(target))
        & classes.knowledge_date.le(target)
    ].sort_values("knowledge_date").drop_duplicates("security_id", keep="last")
    return result.drop(columns=["sector", "sub_industry"], errors="ignore").merge(
        classes[["security_id", "sector", "sub_industry"]], on="security_id", how="left", validate="one_to_one",
    )
