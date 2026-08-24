#!/usr/bin/env python3
"""Build and optionally publish US_LIQUID_5M from coverage and PIT rules."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import sys
import time
from uuid import uuid4

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.data.broad_coverage import BroadCoverageReader  # noqa: E402
from src.data.derived_universe import (  # noqa: E402
    build_liquid_5m_candidate,
    historical_pit_bar_coverage_check,
    roll_forward_liquid_5m_candidate,
)
from src.data.foundation import MarketDataCatalog, MarketDataReader  # noqa: E402
from src.data.membership_state import complete_snapshot_dates  # noqa: E402
from src.data.security_master_store import SecurityMasterStore  # noqa: E402
from src.data.universe_ids import US_EQUITY_COVERAGE, US_LIQUID_5M  # noqa: E402
from src.data.universe_publication import DerivedUniverseStore  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402


def _stage(message: str) -> None:
    print(f"[PIT_STAGE] {message}", file=sys.stderr, flush=True)


def _incremental_inputs_match(previous: object, security_generation: object) -> bool:
    """Allow roll-forward only when the identity authority is unchanged."""
    return (
        getattr(previous, "security_master_generation_id", None)
        == getattr(security_generation, "generation_id", None)
        and getattr(previous, "security_master_manifest_sha256", None)
        == getattr(security_generation, "manifest_sha256", None)
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-version-id")
    parser.add_argument("--output-dir", default="outputs/data_audits/us_liquid_5m_candidates")
    parser.add_argument(
        "--full-rebuild",
        action="store_true",
        help="Ignore an older published PIT version and rebuild every month end.",
    )
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(value) / (1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0)


def run(args: argparse.Namespace) -> tuple[dict, int]:
    started = time.perf_counter()
    _stage("loading authenticated coverage and Security Master")
    catalog = MarketDataCatalog(CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)))
    market_reader = MarketDataReader(catalog=catalog)
    parent = (
        market_reader.require_version(
            US_EQUITY_COVERAGE,
            args.dataset_version_id,
            require_price_semantics=True,
        )
        if args.dataset_version_id
        else market_reader.require_latest(
            US_EQUITY_COVERAGE,
            require_price_semantics=True,
        )
    )
    parent_manifest = market_reader.verify_version(parent)
    security_settings = CONFIG.data.security_master
    security_store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(security_settings.snapshot_dir)),
    )
    security_generation, security_frames = security_store.load_published()
    if (
        parent_manifest.get("security_master_generation_id")
        != security_generation.generation_id
        or parent_manifest.get("security_master_manifest_sha256")
        != security_generation.manifest_sha256
    ):
        raise RuntimeError(
            "coverage version is not bound to the currently authenticated "
            "Security Master generation"
        )
    settings = CONFIG.data.broad_universe
    coverage_reader = BroadCoverageReader(market_reader=market_reader)
    universe_store = DerivedUniverseStore(
        catalog=catalog,
        snapshot_root=CONFIG.abs_path(str(settings.snapshot_dir)),
        market_reader=market_reader,
    )

    def load_window(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
        return coverage_reader.load_bars(
            start=start,
            end=end,
            version=parent,
            columns=["date", "security_id", "ticker", "close", "volume"],
        )

    previous = universe_store.latest(US_LIQUID_5M)
    if (
        args.publish
        and previous is not None
        and previous.target_session == parent.target_session
    ):
        previous_manifest = universe_store.verify(previous)
        prior_checks = {
            str(item.get("name")): item
            for item in previous_manifest.get("quality_checks") or []
            if isinstance(item, dict)
        }
        historical_check = prior_checks.get(
            "historical_pit_daily_bar_coverage"
        )
        historical_gate_passed = bool(
            historical_check and historical_check.get("passed")
        )
        if not historical_gate_passed and not args.full_rebuild:
            raise RuntimeError(
                "same-session PIT publication predates the historical daily "
                "bar-coverage gate; rerun with --full-rebuild"
            )
        input_mismatch = (
            previous.parent_dataset_version_id != parent.version_id
            or previous.security_master_generation_id
            != security_generation.generation_id
            or previous.security_master_manifest_sha256
            != security_generation.manifest_sha256
        )
        if input_mismatch and not args.full_rebuild:
            raise RuntimeError(
                "same-session PIT publication is bound to different inputs; "
                "use --full-rebuild for an explicit repair"
            )
        if historical_gate_passed and not input_mismatch:
            return {
                "status": "NOOP",
                "target_session": parent.target_session.isoformat(),
                "parent_dataset_version_id": parent.version_id,
                "membership_rows": previous.membership_row_count,
                "snapshot_count": previous.snapshot_count,
                "historical_member_count": previous.historical_member_count,
                "publication": previous.to_dict(),
                "report_path": None,
                "report_sha256": None,
                "duration_seconds": round(time.perf_counter() - started, 3),
                "peak_rss_mb": round(_rss_mb(), 3),
            }, 0
    identity_compatible = bool(
        previous is not None
        and _incremental_inputs_match(previous, security_generation)
    )
    incremental = (
        not args.full_rebuild
        and previous is not None
        and previous.target_session < parent.target_session
        and identity_compatible
    )
    build_reason = (
        "EXPLICIT_FULL_REBUILD"
        if args.full_rebuild
        else "NO_PREVIOUS_PUBLICATION"
        if previous is None
        else "SECURITY_MASTER_CHANGED"
        if previous.target_session < parent.target_session and not identity_compatible
        else "INCREMENTAL_INPUTS_MATCH"
        if incremental
        else "FULL_REBUILD_REQUIRED"
    )
    _stage(
        f"building {'incremental' if incremental else 'full'} PIT candidate "
        f"reason={build_reason} target={parent.target_session}"
    )
    if incremental:
        universe_store.verify(previous)
        overlap_days = int(CONFIG.data.foundation.overlap_calendar_days)
        refresh_start = max(
            pd.Timestamp(settings.history_start),
            pd.Timestamp(previous.target_session) - pd.Timedelta(days=overlap_days),
        )
        candidate = roll_forward_liquid_5m_candidate(
            universe_store.load_membership(
                US_LIQUID_5M, version_id=previous.universe_version_id
            ),
            universe_store.load_eligibility(
                US_LIQUID_5M, version_id=previous.universe_version_id
            ),
            security_frames["master"],
            parent_version_id=parent.version_id,
            previous_target_session=previous.target_session,
            target_session=parent.target_session,
            refresh_start=refresh_start,
            history_start=str(settings.history_start),
            research_start=str(settings.research_start),
            symbol_history=security_frames["symbols"],
            min_price=float(settings.min_price),
            min_adv20_usd=float(settings.min_adv20_usd),
            adv_sessions=int(settings.adv_sessions),
            min_valid_sessions=int(settings.min_valid_sessions),
            include_adr=bool(settings.include_adr),
            bar_loader=load_window,
        )
    else:
        candidate = build_liquid_5m_candidate(
            None,
            security_frames["master"],
            parent_version_id=parent.version_id,
            target_session=parent.target_session,
            history_start=str(settings.history_start),
            research_start=str(settings.research_start),
            symbol_history=security_frames["symbols"],
            min_price=float(settings.min_price),
            min_adv20_usd=float(settings.min_adv20_usd),
            adv_sessions=int(settings.adv_sessions),
            min_valid_sessions=int(settings.min_valid_sessions),
            include_adr=bool(settings.include_adr),
            bar_loader=load_window,
        )
    _stage(
        f"candidate complete membership_rows={len(candidate.membership)} "
        f"eligibility_rows={len(candidate.eligibility)}"
    )
    coverage_paths = market_reader.partition_paths(
        parent,
        start=str(settings.research_start),
        end=parent.target_session,
    )
    _stage("running full-history PIT daily bar-coverage gate")
    historical_coverage_check, historical_coverage = historical_pit_bar_coverage_check(
        candidate.membership,
        coverage_paths,
        start=str(settings.research_start),
        end=parent.target_session,
        minimum_coverage=float(
            CONFIG.data.foundation.min_pit_daily_coverage
        ),
    )
    _stage(
        "full-history coverage gate complete "
        f"passed={historical_coverage_check.passed}"
    )
    quality_checks = [*candidate.checks, historical_coverage_check]
    passed = all(check.passed for check in quality_checks)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = (
        Path(args.output_dir).resolve()
        / f"asof={parent.target_session}"
        / f"run={stamp}_{uuid4().hex[:8]}"
    )
    run_dir.mkdir(parents=True)
    membership_path = run_dir / "membership.parquet"
    eligibility_path = run_dir / "eligibility_audit.parquet"
    candidate.membership.to_parquet(membership_path, index=False)
    candidate.eligibility.to_parquet(eligibility_path, index=False)
    publication = None
    if args.publish and passed:
        _stage("publishing immutable PIT universe")
        publication = universe_store.publish(
            universe=US_LIQUID_5M,
            parent_version=parent,
            security_master=security_generation,
            membership=candidate.membership,
            eligibility=candidate.eligibility,
            methodology_version=str(settings.methodology_version),
            checks=quality_checks,
        )
    report = {
        "schema_version": 1,
        "audit": "US_LIQUID_5M_PIT_BUILD",
        "status": "PUBLISHED" if publication else "PASS" if passed else "FAIL",
        "mode": (
            "INCREMENTAL_PUBLISH"
            if publication is not None and incremental
            else "FULL_PUBLISH"
            if publication is not None
            else "INCREMENTAL_CANDIDATE"
            if incremental
            else "FULL_CANDIDATE"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_session": parent.target_session.isoformat(),
        "parent_dataset_version_id": parent.version_id,
        "parent_dataset_manifest_sha256": parent.manifest_checksum_sha256,
        "security_master_generation_id": security_generation.generation_id,
        "security_master_manifest_sha256": security_generation.manifest_sha256,
        "methodology": candidate.methodology,
        "build_reason": build_reason,
        "membership": {
            "path": str(membership_path),
            "sha256": _sha256(membership_path),
            "rows": len(candidate.membership),
            "snapshot_count": int(len(complete_snapshot_dates(candidate.membership))),
            "removal_event_count": int((~candidate.membership["active"]).sum()),
            "historical_member_count": int(candidate.membership["security_id"].nunique()),
        },
        "eligibility": {
            "path": str(eligibility_path),
            "sha256": _sha256(eligibility_path),
            "rows": len(candidate.eligibility),
        },
        "quality_checks": [check.to_dict() for check in quality_checks],
        "historical_pit_daily_bar_coverage": historical_coverage,
        "publication": publication.to_dict() if publication else None,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mb": round(_rss_mb(), 3),
    }
    report_path = run_dir / "audit.json"
    atomic_save_json(report, report_path)
    summary = {
        "status": report["status"],
        "target_session": report["target_session"],
        "parent_dataset_version_id": parent.version_id,
        "membership_rows": report["membership"]["rows"],
        "snapshot_count": report["membership"]["snapshot_count"],
        "historical_member_count": report["membership"]["historical_member_count"],
        "publication": report["publication"],
        "report_path": str(report_path),
        "report_sha256": _sha256(report_path),
        "duration_seconds": report["duration_seconds"],
        "peak_rss_mb": report["peak_rss_mb"],
    }
    return summary, 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result, exit_code = run(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            "status={status} target={target_session} snapshots={snapshot_count} "
            "historical_members={historical_member_count} report={report_path}".format(
                **result
            )
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
