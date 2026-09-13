"""Exercise local fixes against read-only SG inputs; publish nothing."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
    parser.add_argument("--skip-xmax", action="store_true", help="Repeat read-only checks without refetching full XMAX history")
    args = parser.parse_args()
    production = args.production_root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)

    import src.config as config
    config.PROJECT_ROOT = production
    config.CONFIG.reload(production / "configs/default.yaml")
    import pandas as pd
    from scripts.diagnose_cup_handle_data_gaps import audit_provider_gaps
    from src.breakouts.daily_data import load_breakout_daily_dataset
    from src.breakouts.live.candidates import build_daily_candidate_snapshot
    from src.breakouts.live.cup_handle_replay import replay_cup_handle
    from src.breakouts.live.settings import IntradayMonitorSettings
    from src.data.broad_coverage import BroadCoverageReader, select_coverage_securities, normalize_coverage_bars
    from src.data.broad_history_repair import fetch_replacement, file_sha256, load_repair_rules
    from src.data.foundation import MarketDataReader
    from src.data.fmp import get_coverage_historical_ohlcv
    from src.data.membership_state import resolve_membership_asof
    from src.data.security_master_store import SecurityMasterStore
    from src.data.universe_publication import DerivedUniverseStore
    from src.operations.evidence import safe_text
    from src.utils.env import load_local_env

    load_local_env('/etc/quant/intraday-momentum-monitor.env')
    settings = IntradayMonitorSettings.load()
    assert settings.cup_handle_delivery_enabled is False
    reader = MarketDataReader()
    parent = reader.require_latest("US_EQUITY_COVERAGE", require_price_semantics=True)
    parent_manifest = reader.verify_version(parent)
    store = SecurityMasterStore(reader.catalog.path, config.CONFIG.abs_path(str(config.CONFIG.data.security_master.snapshot_dir)))
    current, frames = store.load_published()
    universe_store = DerivedUniverseStore(catalog=reader.catalog,
        snapshot_root=config.CONFIG.abs_path(str(config.CONFIG.data.broad_universe.snapshot_dir)), market_reader=reader)
    pit = universe_store.require_latest("US_LIQUID_5M")
    identities = {
        "TEAD": "sec_0d511e5228f1544e80b30a6fcfcb4dc7", "BGMS": "sec_bea58d6552535699bd937bc8b816707b",
        "STEX": "sec_d684f7d38176556a9bd5f7547716c305", "XMAX": "sec_3fe2e95c9faa5f7f927029ac1de11ea2",
    }
    membership = universe_store.load_membership("US_LIQUID_5M", version_id=pit.universe_version_id)
    members = resolve_membership_asof(membership, pd.Timestamp("2026-09-10"))
    report = {"observed_at": datetime.now(timezone.utc).isoformat(), "production_code_changed": False,
              "coverage_published": False, "cup_delivery_enabled": False, "counts_for_shadow_promotion": False,
              "coverage_version": parent.version_id, "pit_version": pit.universe_version_id,
              "latest_master": current.generation_id, "bound_master": parent_manifest["security_master_generation_id"],
              "affected_current_pit_members": [ticker for ticker, sid in identities.items() if sid in set(members.security_id)]}

    def cup_state():
        with sqlite3.connect((production / "outputs/intraday_momentum_monitor/state.sqlite3").as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            state = {}
            for table in ("cup_handle_cycles", "cup_handle_evaluations", "cup_handle_session_observations", "cup_handle_data_gaps"):
                rows = [dict(r) for r in conn.execute("SELECT * FROM " + table + " WHERE algorithm_version=? ORDER BY rowid",
                                                    ("daily-cup-5m-handle-shadow-v3",))]
                state[table] = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
            gaps = [dict(r) for r in conn.execute("SELECT * FROM cup_handle_data_gaps WHERE session_date=? AND algorithm_version=?",
                                               ("2026-09-11", "daily-cup-5m-handle-shadow-v3"))]
            return state, gaps

    before, gaps = cup_state()
    def attempt(name, action):
        try:
            report[name] = {"status": "COMPLETED", "result": action()}
        except Exception as exc:
            report[name] = {"status": "BLOCKED", "error_type": type(exc).__name__, "error": safe_text(str(exc), limit=2000)}

    def replay():
        dataset = load_breakout_daily_dataset(requested_universe="US_ACTIVE", ticker_selector=lambda _: ["MDB"], end="2026-08-11")
        minute_path = production / "data/raw/intraday/1min/MDB.parquet"
        minutes = pd.read_parquet(minute_path)
        value = replay_cup_handle({"MDB": dataset.frame("MDB")}, {"MDB": minutes}, settings=settings, end="2026-08-11")
        value.update(data_contract=dataset.contract.to_dict(), minute_sha256=file_sha256(minute_path), counts_for_shadow_promotion=False)
        (output / "mdb-v3-replay.json").write_text(json.dumps(value, indent=2, default=str))
        return {k: value[k] for k in ("algorithm_version", "signal_count", "false_positive_rate_proxy", "evaluated_sessions", "evaluated_completed_5m_bars", "max_bar_count", "detection_p95_ms")}
    attempt("mdb_replay", replay)
    attempt("new_candidate", lambda: build_daily_candidate_snapshot(settings, session_date="2026-09-14", source_session="2026-09-11"))

    source_root = production / "outputs/data_audits/cup_resilience_20260913"
    def frozen_minutes(ticker, *, interval, start, end):
        assert start == end == "2026-09-11"
        return pd.read_parquet(source_root / f"{ticker}_{interval}.parquet")
    minute_report = audit_provider_gaps(gaps, fetch=frozen_minutes)
    (output / "minute-comparison.json").write_text(json.dumps(minute_report, indent=2, default=str))
    report["minute_comparison"] = [{k: row[k] for k in ("ticker", "gap_start", "classification", "row_counts", "bucket_evidence")} for row in minute_report["results"]]

    def certify_xmax():
        assert parent.version_id == "76e68448ccea48f5b5e1dbf871c9f6c9"
        assert current.generation_id == "5c738854ad504f1c863c47cf15bb4a63"
        cfg = config.CONFIG.data.broad_coverage
        universe = select_coverage_securities(frames["master"], history_start=str(cfg.history_start), target_session=pd.Timestamp("2026-09-11"),
            allowed_asset_types=list(cfg.allowed_asset_types), benchmark_tickers=list(cfg.benchmark_tickers), history_policy=frames.get("history_policy"))
        rules, approved, rule_contract = load_repair_rules(ROOT / "configs/full_history_repair_rules.yaml", catalog=reader.catalog, market_reader=reader)
        sid = identities["XMAX"]
        previous = BroadCoverageReader(market_reader=reader).load_bars(security_ids=[sid], version=parent)
        raw = get_coverage_historical_ohlcv("XMAX", "2026-09-10", "2026-09-11")
        assert raw is not None and not raw.empty
        raw.to_parquet(output / "xmax-recent.parquet")
        recent = raw.reset_index().assign(security_id=sid, ticker="XMAX")
        recent = normalize_coverage_bars(recent, target_session=pd.Timestamp("2026-09-11"), ingestion_run_id="isolated-audit", source="FMP")
        binding = {"parent_dataset_version_id": parent.version_id, "parent_manifest_sha256": parent.manifest_checksum_sha256,
                   "security_master_generation_id": current.generation_id, "security_master_manifest_sha256": current.manifest_sha256,
                   "target_session": "2026-09-11", "purpose": "ISOLATED_SINGLE_SECURITY_CERTIFICATION_NOT_PUBLICATION",
                   "recent_sha256": file_sha256(output / "xmax-recent.parquet"), "reviewed_rules": rule_contract}
        path, proof = fetch_replacement(cache_dir=output / "repair-cache", contract=binding, security_id=sid,
            universe=universe, symbols=frames["symbols"], previous=previous, recent=recent,
            history_start=str(cfg.history_start), target=pd.Timestamp("2026-09-11"), fetcher=get_coverage_historical_ohlcv,
            query_mappings=rules, approved_quarantine=approved, rules_contract=rule_contract)
        return {"artifact": str(path), "sha256": file_sha256(path), "proof": proof, "publishable": False}
    if not args.skip_xmax:
        attempt("xmax_certification", certify_xmax)
    report["v3_tables_unchanged"] = cup_state()[0] == before
    assert report["v3_tables_unchanged"]
    assert reader.require_latest("US_EQUITY_COVERAGE").version_id == parent.version_id
    assert store.published_generation().generation_id == current.generation_id
    (output / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(json.dumps({k: v for k, v in report.items() if k != "minute_comparison"}, default=str))


if __name__ == "__main__":
    main()
