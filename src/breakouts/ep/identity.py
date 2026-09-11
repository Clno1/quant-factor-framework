"""Read-only reuse of versioned security identities; never publishes a master."""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

from .catalyst import event_window
from .models import NEW_YORK, ticker, timestamp


def current_profile(record, now):
    if not record or record.get('status') != 'OK' or not record.get('profile'):
        return False
    try:
        observed = datetime.fromisoformat(record['observed_at'])
        timestamp(observed)
        if observed > now:
            return False
        age = now - observed
        profile = record['profile']
        if profile.get('identity_source') in {'PUBLISHED_SECURITY_MASTER', 'FMP_BULK_INPUT'}:
            source_time = datetime.fromisoformat(profile['identity_source_received_at'])
            timestamp(source_time)
            age = now - source_time
            target = date.fromisoformat(profile['identity_target_session'])
            required = event_window(now)['start'].astimezone(NEW_YORK).date()
            return timedelta(0) <= age <= timedelta(hours=96) and required <= target <= now.astimezone(NEW_YORK).date()
        return timedelta(0) <= age < timedelta(hours=24)
    except (ValueError, KeyError, TypeError):
        return False


@dataclass
class IdentitySnapshot:
    observed_at: datetime
    profiles: dict = field(default_factory=dict)
    rejected: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)

    def profile(self, symbol, now):
        profile = self.profiles.get(symbol)
        row = {'status': 'OK', 'profile': profile, 'observed_at': timestamp(self.observed_at)}
        return profile if current_profile(row, now) else None


def _normalize(frame, *, observed_at, target_session, source, version, now):
    from src.data.fmp import infer_us_security_asset_type
    required = {'ticker', 'name', 'asset_type', 'exchange', 'trading_status', 'cik', 'cusip', 'isin'}
    if required - set(frame.columns):
        raise ValueError('BULK_IDENTITY_COLUMNS_MISSING')
    snapshot = IdentitySnapshot(observed_at, provenance={'source': source, 'version': version,
                                                        'target_session': str(target_session)})
    _require_current(observed_at, target_session, now)
    for row in frame[sorted(required)].astype(object).fillna('').to_dict('records'):
        try:
            symbol = ticker(row['ticker'])
        except ValueError:
            continue
        asset = str(row['asset_type']).upper()
        inferred = infer_us_security_asset_type(ticker=symbol, name=str(row['name']),
                    is_adr=asset == 'ADR', is_etf=asset == 'ETF', is_fund=asset == 'FUND')
        if asset in {'STOCK', 'ADR'} and inferred not in {'STOCK', 'ADR'}:
            asset = inferred
        profile = {'ticker': symbol, 'name': str(row['name']).strip(), 'asset_type': asset,
                   'exchange': str(row['exchange']).upper(), 'cik': str(row['cik']).strip(),
                   'cusip': str(row['cusip']).strip(), 'isin': str(row['isin']).strip(),
                   'is_actively_trading': str(row['trading_status']).upper() == 'ACTIVE',
                   'identity_source': source, 'identity_version': version,
                   'identity_source_received_at': timestamp(observed_at),
                   'identity_target_session': str(target_session)}
        if symbol in snapshot.rejected:
            continue
        if symbol in snapshot.profiles and snapshot.profiles[symbol] != profile:
            snapshot.profiles.pop(symbol)
            snapshot.rejected[symbol] = 'AMBIGUOUS_BULK_IDENTITY'
        elif not profile['name']:
            snapshot.profiles.pop(symbol, None)
            snapshot.rejected[symbol] = 'BULK_IDENTITY_NAME_MISSING'
        else:
            snapshot.profiles[symbol] = profile
    return snapshot


def _require_current(observed, target, now):
    record = {'status': 'OK', 'observed_at': timestamp(observed), 'profile': {
        'identity_source': 'FMP_BULK_INPUT', 'identity_source_received_at': timestamp(observed),
        'identity_target_session': str(target)}}
    if not current_profile(record, now):
        raise ValueError('BULK_IDENTITY_SNAPSHOT_STALE_OR_FUTURE')


def _diagnostic(source, exc):
    code = str(exc)
    return {'source': source, 'status': code if re.fullmatch(r'BULK_IDENTITY_[A-Z_]+', code) else type(exc).__name__}


def from_published(store, now):
    generation, frames = store.load_published()
    frame = frames['master'].rename(columns={'current_ticker': 'ticker', 'primary_exchange': 'exchange'})
    return _normalize(frame, observed_at=generation.created_at, target_session=generation.target_session,
                      source='PUBLISHED_SECURITY_MASTER', version=generation.generation_id, now=now)


def from_provider_manifest(path, now):
    import pandas as pd
    path = Path(path).resolve()
    if path.stat().st_size > 1_000_000:
        raise ValueError('BULK_IDENTITY_MANIFEST_TOO_LARGE')
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get('schema_version') != 1 or manifest.get('source') != 'FMP_SECURITY_MASTER_INPUTS':
        raise ValueError('BULK_IDENTITY_MANIFEST_UNSUPPORTED')
    observed = datetime.fromisoformat(manifest['created_at'])
    timestamp(observed)
    _require_current(observed, manifest['target_session'], now)
    artifact = manifest['artifacts']['profiles']
    file = (path.parent / artifact['file']).resolve()
    if file.parent != path.parent or file.suffix != '.parquet' or file.stat().st_size > 100_000_000:
        raise ValueError('BULK_IDENTITY_ARTIFACT_PATH_REJECTED')
    if hashlib.sha256(file.read_bytes()).hexdigest() != artifact['sha256']:
        raise ValueError('BULK_IDENTITY_HASH_MISMATCH')
    frame = pd.read_parquet(file)
    if len(frame) != artifact['rows'] or len(frame) > 200_000:
        raise ValueError('BULK_IDENTITY_ROW_COUNT_MISMATCH')
    return _normalize(frame, observed_at=observed, target_session=manifest['target_session'],
                      source='FMP_BULK_INPUT', version=hashlib.sha256(raw).hexdigest(), now=now)


def load_identity_snapshot(now, *, catalog_path=None, snapshot_root=None, source_root=None):
    import duckdb
    from src.config import CONFIG
    from src.data.security_master_store import SecurityMasterStore
    catalog_path = catalog_path or CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path))
    snapshot_root = snapshot_root or CONFIG.abs_path(str(CONFIG.data.security_master.snapshot_dir))
    source_root = Path(source_root or CONFIG.abs_path('outputs/data_audits/security_master_candidates'))
    errors = []
    try:
        result = from_published(SecurityMasterStore(catalog_path, snapshot_root), now)
        result.diagnostics = errors
        return result
    except (OSError, ValueError, RuntimeError, KeyError, duckdb.Error) as exc:
        errors.append(_diagnostic('PUBLISHED_SECURITY_MASTER', exc))
    # Provider inputs can remain useful even if a separate historical-identity
    # publication failed. They are not treated as a published Security Master.
    paths = sorted(source_root.glob('asof=*/run=*/provider_sources/manifest.json'), reverse=True)
    for path in paths[:20]:
        try:
            result = from_provider_manifest(path, now)
            result.diagnostics = errors
            return result
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            errors.append(_diagnostic('FMP_BULK_INPUT', exc))
    return IdentitySnapshot(now, diagnostics=errors, provenance={'source': 'UNAVAILABLE'})
