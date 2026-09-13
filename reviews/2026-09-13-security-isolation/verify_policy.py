"""Revalidate frozen SG source bytes and the isolation budget; publish nothing."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prod, output = args.production_root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    import src.config as config
    config.PROJECT_ROOT = prod
    config.CONFIG.reload(prod / "configs/default.yaml")
    import pandas as pd
    from src.data.broad_coverage import BroadCoverageReader, select_coverage_securities, normalize_coverage_bars, coverage_alias_intervals
    from src.data.broad_history_repair import file_sha256, inherit_quarantine, load_repair_rules, validate_replacement
    from src.data.foundation import MarketDataReader
    from src.data.security_availability import MissingAuthenticatedHistory, REASON, build_availability
    from src.data.security_master_store import SecurityMasterStore
    from src.breakouts.live.settings import IntradayMonitorSettings
    from src.utils.env import load_local_env
    load_local_env('/etc/quant/intraday-momentum-monitor.env')
    assert IntradayMonitorSettings.load().cup_handle_delivery_enabled is False

    def state_hashes():
        with sqlite3.connect((prod / 'outputs/intraday_momentum_monitor/state.sqlite3').as_uri() + '?mode=ro', uri=True) as conn:
            return {name: hashlib.sha256(json.dumps(conn.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()).encode()).hexdigest()
                    for name in ['cup_handle_cycles', 'cup_handle_evaluations', 'cup_handle_session_observations', 'cup_handle_data_gaps']}
    before = state_hashes()
    reader = MarketDataReader()
    parent = reader.require_latest('US_EQUITY_COVERAGE', require_price_semantics=True)
    store = SecurityMasterStore(reader.catalog.path, config.CONFIG.abs_path(str(config.CONFIG.data.security_master.snapshot_dir)))
    generation, frames = store.load_published()
    cfg = config.CONFIG.data.broad_coverage
    target = pd.Timestamp('2026-09-11')
    scope = select_coverage_securities(frames['master'], history_start=str(cfg.history_start), target_session=target,
        allowed_asset_types=list(cfg.allowed_asset_types), benchmark_tickers=list(cfg.benchmark_tickers), history_policy=frames.get('history_policy'))
    _, approved, _ = load_repair_rules(ROOT / 'configs/full_history_repair_rules.yaml', catalog=reader.catalog, market_reader=reader)
    cache = prod / 'data/lake/staging/us_equity_coverage_incremental/asof=2026-09-11/provider_cache/binding=6151dfe7592b274cb278ad8d79ec77264962a507b73e1890b0c9a81fece1d23e/full_security_repair'
    identities = {'TEAD': 'sec_0d511e5228f1544e80b30a6fcfcb4dc7', 'BGMS': 'sec_bea58d6552535699bd937bc8b816707b', 'STEX': 'sec_d684f7d38176556a9bd5f7547716c305'}
    errors, sources = [], []
    for ticker, sid in identities.items():
        paths = list((cache / sid).glob('*/attempt=*/failure.json'))
        path = max(paths, key=lambda p: p.stat().st_mtime)
        source = json.loads(path.read_text())
        binding = source['contract']
        assert binding['parent_dataset_version_id'] == parent.version_id
        assert binding['parent_manifest_sha256'] == parent.manifest_checksum_sha256
        assert binding['security_master_generation_id'] == generation.generation_id
        assert binding['security_master_manifest_sha256'] == generation.manifest_sha256
        assert binding['target_session'] == str(target.date())
        pieces = []
        for artifact in source['raw_artifacts']:
            raw_path = (path.parent.parent / artifact['path']).resolve()
            assert raw_path.is_relative_to(cache) and file_sha256(raw_path) == artifact['sha256']
            frame = pd.read_parquet(raw_path).reset_index()
            if 'date' not in frame:
                frame = frame.rename(columns={frame.columns[0]: 'date'})
            assert pd.to_datetime(frame.date).between(pd.Timestamp(artifact['start']), pd.Timestamp(artifact['end'])).all()
            pieces.append(frame.assign(security_id=sid, ticker=artifact['historical_ticker']))
        combined = normalize_coverage_bars(pd.concat(pieces, ignore_index=True), target_session=target,
            ingestion_run_id='isolation-policy-readonly', source='FROZEN_CANONICAL_SOURCE_REVALIDATION')
        previous = BroadCoverageReader(market_reader=reader).load_bars(security_ids=[sid], version=parent)
        combined, _ = inherit_quarantine(combined, approved=approved, previous=previous)
        aliases = coverage_alias_intervals(scope.loc[scope.security_id.eq(sid)], frames['symbols'],
            history_start=str(cfg.history_start), target_session=target)
        try:
            validate_replacement(combined, security_id=sid, previous=previous, recent=combined.iloc[:0], aliases=aliases, target=target)
            raise AssertionError('expected frozen-source history gap was not reproduced')
        except MissingAuthenticatedHistory as exc:
            evidence = {**source, 'error_code': REASON, 'missing_dates': exc.missing_dates,
                        'source_failure_sha256': file_sha256(path), 'purpose': 'PARENT_HISTORY_REVALIDATION_ONLY_NOT_PUBLICATION'}
            saved = output / f'{ticker}-revalidated.json'
            saved.write_text(json.dumps(evidence, indent=2))
            errors.append({'security_id': sid, 'ticker': ticker, 'error_code': REASON,
                'missing_dates': exc.missing_dates, 'isolation_evidence': {'path': str(saved), 'sha256': file_sha256(saved), 'record': evidence}})
            sources.append({'ticker': ticker, 'missing_dates': len(exc.missing_dates), 'source_failure': str(path),
                            'source_sha256': file_sha256(path), 'present_rows_revalidated': len(combined)})
    availability = build_availability(scope, target_session=target, errors=errors,
        parent_version_id=parent.version_id, parent_manifest_sha256=parent.manifest_checksum_sha256)
    assert before == state_hashes()
    assert reader.require_latest('US_EQUITY_COVERAGE').version_id == parent.version_id
    report = {'publication_attempted': False, 'production_code_changed': False, 'source_requests': 0,
        'counts_for_shadow_promotion': False, 'cup_delivery_enabled': False, 'live_tables_unchanged': True,
        'coverage_version': parent.version_id, 'sources': sources, 'security_availability': availability,
        'limitation': 'Revalidates missing parent history and isolation eligibility, not full-scope publication or new candidate readiness.'}
    (output / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({**{k: v for k, v in report.items() if k != 'security_availability'},
        'availability_status': availability['status'], 'expected_count': availability['expected_count'],
        'isolated_count': len(errors), 'isolated_ratio': len(errors) / len(scope)}))


if __name__ == '__main__':
    main()
