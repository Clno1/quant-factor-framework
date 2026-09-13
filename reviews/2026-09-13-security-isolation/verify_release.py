"""Verify the formal chain and prepare a dated candidate, never intraday results."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--coverage', required=True)
    p.add_argument('--session', required=True)
    p.add_argument('--source', required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--save-candidate', action='store_true')
    a = p.parse_args()
    from src.config import CONFIG
    import pandas as pd
    from src.data.foundation import MarketDataReader
    from src.data.broad_coverage import BroadCoverageReader
    from src.data.security_availability import availability_from_manifest, unavailable_ids
    from src.data.universe_publication import DerivedUniverseStore
    from src.breakouts.live.settings import IntradayMonitorSettings
    from src.breakouts.live.candidates import build_daily_candidate_snapshot
    from src.breakouts.live.state import IntradayMonitorState
    from src.breakouts.live.session import expected_source_session
    from src.breakouts.broad_daily_data import validate_breakout_daily_data_contract
    from src.utils.env import load_local_env
    load_local_env('/etc/quant/intraday-momentum-monitor.env')
    settings = IntradayMonitorSettings.load()
    assert settings.cup_handle_delivery_enabled is False
    a.output.mkdir(parents=True, exist_ok=False)

    def live_hashes():
        with sqlite3.connect(settings.state_path.as_uri() + '?mode=ro', uri=True) as conn:
            return {n: hashlib.sha256(json.dumps(conn.execute('SELECT * FROM ' + n + ' ORDER BY rowid').fetchall()).encode()).hexdigest()
                    for n in ['cup_handle_cycles', 'cup_handle_evaluations', 'cup_handle_session_observations', 'cup_handle_data_gaps']}

    before = live_hashes()
    reader = MarketDataReader()
    parent = reader.require_latest('US_EQUITY_COVERAGE', require_price_semantics=True)
    assert parent.version_id == a.coverage and str(parent.target_session) == a.source
    manifest = reader.verify_version(parent)
    availability = availability_from_manifest(manifest)
    assert availability and availability['expected_count'] == 8026
    isolated = unavailable_ids(availability)
    assert {r['ticker'] for r in availability['unavailable']} == {'TEAD', 'BGMS', 'STEX'}
    assert BroadCoverageReader(market_reader=reader).load_bars(security_ids=sorted(isolated), version=parent).empty
    store = DerivedUniverseStore(catalog=reader.catalog,
        snapshot_root=CONFIG.abs_path(str(CONFIG.data.broad_universe.snapshot_dir)), market_reader=reader)
    pit = store.require_latest('US_LIQUID_5M')
    assert pit.parent_dataset_version_id == parent.version_id
    assert store.verify(pit)['security_availability'] == availability
    # The complete artifacts were hash-verified above; predicate reads keep this
    # independent audit from retaining a million rows beside the candidate build.
    membership = pd.read_parquet(CONFIG.abs_path(pit.membership_path),
        columns=['security_id'], filters=[('security_id', 'in', sorted(isolated))])
    assert membership.empty
    affected = pd.read_parquet(CONFIG.abs_path(pit.eligibility_path),
        columns=['security_id', 'eligible', 'reason_codes'],
        filters=[('security_id', 'in', sorted(isolated))])
    assert set(affected.security_id.astype(str)) == isolated
    assert not affected.eligible.any()
    assert affected.reason_codes.eq('UPSTREAM_SECURITY_ISOLATED').all()
    assert expected_source_session(a.session) == a.source
    snapshot = build_daily_candidate_snapshot(settings, session_date=a.session, source_session=a.source)
    assert validate_breakout_daily_data_contract(snapshot['data_contract']).version_id == parent.version_id
    assert snapshot['data_contract']['coverage']['security_availability'] == availability
    assert snapshot['cup_handle_daily']['algorithm_version'] == 'daily-cup-5m-handle-shadow-v3'
    assert not {r['ticker'] for r in snapshot['rows']} & {'TEAD', 'BGMS', 'STEX'}
    (a.output / 'candidate.json').write_text(json.dumps(snapshot, indent=2))
    assert reader.require_latest('US_EQUITY_COVERAGE').version_id == parent.version_id
    assert store.require_latest('US_LIQUID_5M').universe_version_id == pit.universe_version_id
    if a.save_candidate:
        IntradayMonitorState(settings.state_path).save_candidate_snapshot(snapshot)
    after = live_hashes()
    assert before == after, 'historical cup tables changed'
    report = {'coverage_version': parent.version_id, 'coverage_manifest_sha256': parent.manifest_checksum_sha256,
        'pit_version': pit.universe_version_id, 'pit_manifest_sha256': pit.manifest_sha256,
        'availability_sha256': availability['sha256'], 'expected_count': availability['expected_count'],
        'isolated_count': len(isolated), 'isolated_eligibility_rows': len(affected),
        'candidate': {k: v for k, v in snapshot.items() if k not in ('rows', 'data_contract')},
        'candidate_saved': a.save_candidate, 'candidate_sha256': hashlib.sha256((a.output / 'candidate.json').read_bytes()).hexdigest(),
        'historical_cup_tables_unchanged': True, 'live_table_hashes': after,
        'counts_for_shadow_promotion': False, 'delivery_enabled': False}
    (a.output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
