"""Current cap observations, owned by rotation, never historical factor data."""
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.config import PROJECT_ROOT
from src.group_analytics.adapters import _atomic_json, _exclusive_file_lock
from .reference import bound_reference, classified_members, current_members
from .store import encoded

SCHEMA = "rotation.cap-observation.v1"
MAX_AGE_HOURS = 72


def default_root():
    return PROJECT_ROOT / "data/reference/group_analytics/rotation/capitalization"


def build_observation(members, provider, *, version_id, generation_id, source_session, captured_at):
    captured = pd.Timestamp(captured_at)
    if captured.tzinfo is None:
        raise ValueError("Capture time needs timezone")
    required = {"ticker", "market_cap", "exchange"}
    if not required.issubset(provider.columns) or provider.ticker.duplicated().any():
        raise ValueError("Invalid cap observation schema or duplicate ticker")
    source = provider[list(required)].rename(columns={"exchange": "provider_exchange"}).copy()
    source["market_cap"] = pd.to_numeric(source.market_cap, errors="coerce")
    source.loc[~source.market_cap.gt(0) | source.market_cap.isin([float('inf'), -float('inf')]), "market_cap"] = float('nan')
    eligible = current_members(members)
    eligible = eligible.loc[eligible.coverage_role.ne("BENCHMARK_ONLY")].copy()
    joined = eligible.merge(source, on="ticker", how="left", validate="one_to_one")
    # A current ticker is resolved only within the frozen US listing generation.
    joined.loc[joined.primary_exchange.ne(joined.provider_exchange), "market_cap"] = float('nan')
    good = joined.loc[joined.market_cap.notna()]
    if good.empty:
        raise ValueError("No resolved positive caps")
    return {
        "schema_version": SCHEMA, "source": "FMP company-screener current observation",
        "source_session": str(source_session), "dataset_version_id": version_id,
        "security_master_generation_id": generation_id, "captured_at": captured.isoformat(),
        "provider_effective_at": None, "point_in_time": False,
        "identity_policy": "CURRENT_TICKER_AND_EXCHANGE_BOUND_TO_MASTER",
        "provider_sha256": hashlib.sha256(encoded(provider.to_dict('records'))).hexdigest(),
        "eligible": len(joined), "mapped": len(good), "coverage": len(good) / len(joined),
        "members": good[["security_id", "ticker", "market_cap"]].to_dict("records"),
    }


def refresh_observation(*, reader=None, provider_loader=None, root=None, now=None):
    from src.data.fmp import get_us_active_equities
    from src.data.foundation import MarketDataReader
    from src.data.universe_ids import US_EQUITY_COVERAGE
    from src.group_analytics.calendar import latest_completed_session
    from src.data.security_availability import availability_from_manifest, unavailable_ids
    market = reader or MarketDataReader()
    version, frames, generation = bound_reference(market)
    session = latest_completed_session(now=now).date().isoformat()
    if str(version.target_session) != session:
        raise ValueError("Coverage reference is stale")
    members = market.load_universe(US_EQUITY_COVERAGE, current_only=False, version=version)
    manifest = market.verify_version(version, require_price_semantics=True, verify_partition_children=False)
    members = members.loc[~members.security_id.isin(unavailable_ids(availability_from_manifest(manifest)))]
    provider = (provider_loader or get_us_active_equities)()
    result = build_observation(members, provider, version_id=version.version_id,
                               generation_id=generation.generation_id, source_session=session,
                               captured_at=now or pd.Timestamp.now(tz="UTC"))
    root = Path(root or default_root())
    digest = hashlib.sha256(encoded(result)).hexdigest()
    with _exclusive_file_lock(root / '.lock'):
        latest = market.require_latest(US_EQUITY_COVERAGE, require_price_semantics=True, verify_partition_children=False)
        if latest.version_id != version.version_id:
            raise ValueError("Coverage moved during cap observation")
        try:
            previous = load_observation(version, root=root, validate_age=False)
        except (OSError, ValueError, KeyError, TypeError):
            previous = None
        if previous and pd.Timestamp(previous['captured_at']) > pd.Timestamp(result['captured_at']):
            raise ValueError("Newer cap observation already exists")
        path = root / 'observations' / (digest + '.json')
        if not path.exists():
            _atomic_json(path, result)
        _atomic_json(root / 'latest.json', {'sha256': digest})
    return result


def load_observation(version, *, root=None, now=None, validate_age=True):
    root = Path(root or default_root())
    pointer = json.loads((root / 'latest.json').read_text())
    digest = str(pointer['sha256'])
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError("Invalid cap pointer")
    data = json.loads((root / 'observations' / (digest + '.json')).read_text())
    if hashlib.sha256(encoded(data)).hexdigest() != digest or data.get('schema_version') != SCHEMA:
        raise ValueError("Invalid cap checksum/schema")
    if data.get('dataset_version_id') != version.version_id or data.get('source_session') != str(version.target_session):
        raise ValueError("Cap version mismatch")
    captured = pd.Timestamp(data['captured_at'])
    current = pd.Timestamp(now or pd.Timestamp.now(tz='UTC'))
    if captured.tzinfo is None or current.tzinfo is None or (validate_age and not pd.Timedelta(0) <= current - captured <= pd.Timedelta(hours=MAX_AGE_HOURS)):
        raise ValueError("Stale or future cap observation")
    return data


def heatmap_members(market, version, *, root=None, now=None):
    from src.data.universe_ids import US_EQUITY_COVERAGE
    from src.data.security_availability import availability_from_manifest, unavailable_ids
    _, frames, generation = bound_reference(market, version)
    members = classified_members(market.load_universe(US_EQUITY_COVERAGE, current_only=False, version=version), frames, version.target_session)
    manifest = market.verify_version(version, require_price_semantics=True, verify_partition_children=False)
    isolated = unavailable_ids(availability_from_manifest(manifest))
    members = members.loc[~members.security_id.isin(isolated)]
    caps = load_observation(version, root=root, now=now)
    if caps['security_master_generation_id'] != generation.generation_id:
        raise ValueError("Cap identity generation mismatch")
    values = pd.DataFrame(caps['members'])
    members = members.merge(values, on=['security_id', 'ticker'], how='left', validate='one_to_one')
    return members, {k: caps[k] for k in ('captured_at', 'source', 'provider_effective_at', 'coverage', 'mapped', 'eligible')}
