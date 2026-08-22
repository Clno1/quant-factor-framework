#!/usr/bin/env python3
"""Record and summarize exact broad-data shadow checks by target session.

One passing observation proves that the current coverage, PIT universe,
Security Master and all factor partitions are mutually bound and readable.  A
failed attempt is retained for diagnosis but never counts toward the five-day
rollout gate.  This command never changes the web-default switch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG, PROJECT_ROOT  # noqa: E402
from src.data.foundation import MarketDataCatalog, MarketDataReader  # noqa: E402
from src.data.security_master_store import SecurityMasterStore  # noqa: E402
from src.data.universe_ids import US_EQUITY_COVERAGE, US_LIQUID_5M  # noqa: E402
from src.data.universe_publication import DerivedUniverseStore  # noqa: E402
from src.factors.broad_observations import BroadFactorObservationBackend  # noqa: E402
from src.factors.data_publication import FactorDataStore  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402
from src.utils.market_calendar import latest_publishable_xnys_session  # noqa: E402


LEDGER_SCHEMA_VERSION = 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record-current", action="store_true")
    parser.add_argument("--required-sessions", type=int, default=5)
    parser.add_argument(
        "--ledger-path",
        default="outputs/data_audits/broad_shadow_observation.json",
    )
    parser.add_argument(
        "--require-ready",
        action="store_true",
        help="Return 2 until the consecutive-session gate is ready.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _expected_session() -> str:
    return latest_publishable_xnys_session(
        delay_minutes=int(CONFIG.data.foundation.close_delay_minutes)
    ).date().isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"JSON object expected: {path}")
    return payload


def _latest_successful_pipeline_report(target_session: str) -> tuple[Path, dict[str, Any]]:
    directory = (
        PROJECT_ROOT
        / "outputs"
        / "data_audits"
        / "broad_daily_pipeline"
        / f"target={target_session}"
    )
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in directory.glob("run=*.json"):
        try:
            report = _load_json(path)
        except (OSError, json.JSONDecodeError, RuntimeError):
            continue
        stages = report.get("stages")
        names = [item.get("name") for item in stages or [] if isinstance(item, dict)]
        passed = bool(stages) and all(
            isinstance(item, dict)
            and item.get("status") == "SUCCESS"
            and int(item.get("returncode", 1)) == 0
            for item in stages
        )
        if (
            report.get("status") == "SUCCESS"
            and report.get("target_session") == target_session
            and names == [
                "SECURITY_MASTER",
                "US_EQUITY_COVERAGE",
                "US_LIQUID_5M_PIT",
            ]
            and passed
        ):
            candidates.append((path, report))
    if not candidates:
        raise RuntimeError(
            f"no successful broad daily pipeline report for {target_session}"
        )
    return max(candidates, key=lambda item: item[0].stat().st_mtime)


def _coverage_index(version: Any) -> dict[str, Any]:
    path = _project_path(version.bars_path)
    payload = _load_json(path)
    if payload.get("partition_frequency") != "MONTH":
        raise RuntimeError("coverage publication is not month-partitioned")
    entries = payload.get("partitions")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("coverage partition index is empty")
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("year") or not entry.get("month"):
            raise RuntimeError("coverage partition index lacks month identities")
    return payload


def collect_current_observation() -> dict[str, Any]:
    """Perform full child-hash verification and one real ranked query."""
    checked_at = datetime.now(timezone.utc).isoformat()
    catalog = MarketDataCatalog(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path))
    )
    market_reader = MarketDataReader(catalog=catalog)
    coverage = market_reader.require_latest(US_EQUITY_COVERAGE)
    coverage_index = _coverage_index(coverage)

    security_settings = CONFIG.data.security_master
    security_store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(security_settings.snapshot_dir)),
    )
    security_generation, security_frames = security_store.load_published()

    universe_store = DerivedUniverseStore(
        catalog=catalog,
        snapshot_root=CONFIG.abs_path(
            str(CONFIG.data.broad_universe.snapshot_dir)
        ),
        market_reader=market_reader,
    )
    universe_version = universe_store.require_latest(US_LIQUID_5M)
    universe_manifest = universe_store.verify(universe_version)
    universe_checks = {
        str(item.get("name")): item
        for item in universe_manifest.get("quality_checks") or []
        if isinstance(item, dict)
    }
    historical_bar_check = universe_checks.get(
        "historical_pit_daily_bar_coverage"
    )
    if not historical_bar_check or not historical_bar_check.get("passed"):
        raise RuntimeError(
            "PIT universe lacks a passing historical daily bar-coverage gate"
        )
    if universe_version.parent_dataset_version_id != coverage.version_id:
        raise RuntimeError("PIT universe does not bind the latest coverage version")
    if universe_version.target_session != coverage.target_session:
        raise RuntimeError("coverage and PIT universe target sessions differ")

    factor_store = FactorDataStore(
        market_reader=market_reader,
        universe_store=universe_store,
    )
    publication = factor_store.load_publication(verify_partitions=True)
    target_session = coverage.target_session.isoformat()
    if publication.get("target_session") != target_session:
        raise RuntimeError("factor data and coverage target sessions differ")
    if publication.get("parent_dataset_version_id") != coverage.version_id:
        raise RuntimeError("factor data does not bind the latest coverage version")
    if publication.get("universe_version_id") != universe_version.universe_version_id:
        raise RuntimeError("factor data does not bind the latest PIT universe")
    if (
        publication.get("security_master_generation_id")
        != security_generation.generation_id
        or publication.get("security_master_sha256")
        != security_generation.manifest_sha256
    ):
        raise RuntimeError("factor data does not bind the published Security Master")

    expected_factors = sorted(str(value).upper() for value in CONFIG.factors.enabled)
    observed_factors = sorted((publication.get("factors") or {}).keys())
    if observed_factors != expected_factors:
        raise RuntimeError(
            f"factor publication is incomplete: expected={expected_factors} "
            f"observed={observed_factors}"
        )
    factor_partition_count = 0
    for factor_id in observed_factors:
        partitions = factor_store.partition_entries(publication, factor_id)
        if not partitions or any(
            not item.input_fingerprint_sha256 or not item.input_fingerprint_method
            for item in partitions
        ):
            raise RuntimeError(f"{factor_id} has incomplete factor partitions")
        factor_partition_count += len(partitions)

    pipeline_path, pipeline_report = _latest_successful_pipeline_report(
        target_session
    )
    factor_report_path = _project_path(publication["manifest_path"]).parent / "run_report.json"
    factor_report = _load_json(factor_report_path)
    reported_publication = factor_report.get("publication") or {}
    if (
        factor_report.get("status") != "PUBLISHED"
        or factor_report.get("target_session") != target_session
        or reported_publication.get("publication_id")
        != publication.get("publication_id")
    ):
        raise RuntimeError("factor run report does not match the current publication")

    backend = BroadFactorObservationBackend(
        store=factor_store,
        security_loader=lambda: (security_generation, security_frames),
        expected_session=target_session,
    )
    query_factor = observed_factors[0]
    snapshot = backend.snapshot(
        factor_id=query_factor,
        observation_date=target_session,
        status="valid",
        limit=1,
    )
    minimum_cross_section = int(
        CONFIG.data.broad_factor_research.minimum_cross_section
    )
    if (
        snapshot.total_rows < minimum_cross_section
        or snapshot.summary.get("eligible_count", 0) < minimum_cross_section
        or not snapshot.rows
    ):
        raise RuntimeError("authenticated factor query is below the cross-section gate")

    return {
        "status": "PASS",
        "target_session": target_session,
        "checked_at": checked_at,
        "coverage": {
            "version_id": coverage.version_id,
            "manifest_sha256": coverage.manifest_checksum_sha256,
            "bars_index_sha256": coverage.checksum_sha256,
            "partition_frequency": coverage_index["partition_frequency"],
            "partition_count": len(coverage_index["partitions"]),
        },
        "security_master": {
            "generation_id": security_generation.generation_id,
            "manifest_sha256": security_generation.manifest_sha256,
        },
        "universe": {
            "version_id": universe_version.universe_version_id,
            "membership_sha256": universe_version.membership_sha256,
            "eligibility_sha256": universe_version.eligibility_sha256,
            "current_member_count": universe_version.current_member_count,
            "minimum_daily_bar_coverage": (
                historical_bar_check.get("observed", {}).get(
                    "minimum_daily_coverage"
                )
            ),
        },
        "factor_data": {
            "publication_id": publication["publication_id"],
            "generation_id": publication["generation_id"],
            "manifest_sha256": publication["manifest_sha256"],
            "factor_count": len(observed_factors),
            "partition_count": factor_partition_count,
            "query_factor": query_factor,
            "query_valid_rows": snapshot.total_rows,
            "query_eligible_rows": snapshot.summary["eligible_count"],
        },
        "operations": {
            "pipeline_report_path": str(pipeline_path),
            "pipeline_duration_seconds": pipeline_report.get("duration_seconds"),
            "pipeline_peak_rss_mb": pipeline_report.get("peak_rss_mb"),
            "factor_report_path": str(factor_report_path),
            "factor_duration_seconds": factor_report.get("elapsed_seconds"),
            "factor_peak_rss_mb": factor_report.get("peak_rss_mb"),
            "factor_reused_partitions": factor_report.get(
                "reused_partition_count"
            ),
            "factor_computed_partitions": factor_report.get(
                "computed_partition_count"
            ),
        },
    }


def _trailing_session_streak(session_values: list[str]) -> list[str]:
    if not session_values:
        return []
    import exchange_calendars as xcals

    observed = {pd.Timestamp(value).normalize() for value in session_values}
    first = min(observed)
    last = max(observed)
    calendar = xcals.get_calendar(
        "XNYS",
        start=(first - pd.Timedelta(days=7)).date().isoformat(),
        end=(last + pd.Timedelta(days=2)).date().isoformat(),
    )
    sessions = pd.DatetimeIndex(
        calendar.sessions_in_range(first.date().isoformat(), last.date().isoformat())
    )
    if sessions.tz is not None:
        sessions = sessions.tz_localize(None)
    streak: list[str] = []
    for session in reversed(sessions.normalize()):
        if session not in observed:
            break
        streak.append(session.date().isoformat())
    return list(reversed(streak))


def summarize_ledger(
    ledger: dict[str, Any],
    *,
    required_sessions: int,
    expected_session: str,
) -> dict[str, Any]:
    observations = [
        item
        for item in ledger.get("observations") or []
        if isinstance(item, dict) and item.get("status") == "PASS"
    ]
    deduplicated = {
        str(item["target_session"]): item
        for item in observations
        if item.get("target_session")
    }
    ordered = [deduplicated[key] for key in sorted(deduplicated)]
    streak = _trailing_session_streak(list(deduplicated))
    latest_pass = streak[-1] if streak else None
    last_attempt = ledger.get("last_attempt") or {}
    ready = (
        len(streak) >= int(required_sessions)
        and latest_pass == expected_session
        and last_attempt.get("status") == "PASS"
        and last_attempt.get("target_session") == expected_session
    )
    return {
        **ledger,
        "schema_version": LEDGER_SCHEMA_VERSION,
        "required_sessions": int(required_sessions),
        "expected_session": expected_session,
        "observations": ordered,
        "passed_sessions_total": len(ordered),
        "consecutive_passed_sessions": len(streak),
        "consecutive_dates": streak,
        "remaining_sessions": max(0, int(required_sessions) - len(streak)),
        "ready_for_web_default": ready,
        "status": "READY" if ready else "OBSERVING",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "observations": [],
        "failures": [],
        "last_attempt": None,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.required_sessions < 1:
        raise ValueError("required-sessions must be at least 1")
    path = _project_path(args.ledger_path)
    if path.is_file():
        try:
            ledger = _load_json(path)
        except (OSError, json.JSONDecodeError, RuntimeError) as exc:
            print(f"invalid broad shadow ledger: {exc}", file=sys.stderr)
            return 3
        if int(ledger.get("schema_version") or 0) != LEDGER_SCHEMA_VERSION:
            print("unsupported broad shadow ledger schema", file=sys.stderr)
            return 3
    else:
        ledger = _empty_ledger()

    expected = _expected_session()
    current_failed = False
    if args.record_current:
        try:
            observation = collect_current_observation()
            by_session = {
                str(item["target_session"]): item
                for item in ledger.get("observations") or []
                if isinstance(item, dict) and item.get("target_session")
            }
            by_session[observation["target_session"]] = observation
            ledger["observations"] = [by_session[key] for key in sorted(by_session)]
            ledger["last_attempt"] = {
                "status": "PASS",
                "target_session": observation["target_session"],
                "checked_at": observation["checked_at"],
            }
        except Exception as exc:  # noqa: BLE001
            current_failed = True
            failure = {
                "status": "FAIL",
                "target_session": expected,
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            }
            failures = list(ledger.get("failures") or [])
            failures.append(failure)
            ledger["failures"] = failures[-50:]
            ledger["last_attempt"] = failure

    ledger = summarize_ledger(
        ledger,
        required_sessions=args.required_sessions,
        expected_session=expected,
    )
    if args.record_current:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_save_json(ledger, path)
    ledger["ledger_path"] = str(path)
    if args.json:
        print(json.dumps(ledger, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"broad shadow: {ledger['status']} "
            f"streak={ledger['consecutive_passed_sessions']}/"
            f"{ledger['required_sessions']} remaining={ledger['remaining_sessions']}"
        )
        print(f"ledger: {path}")
        if current_failed:
            print(ledger["last_attempt"]["error"], file=sys.stderr)
    if current_failed:
        return 2
    if args.require_ready and not ledger["ready_for_web_default"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
