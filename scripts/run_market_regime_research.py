#!/usr/bin/env python3
"""
Prepare and build the broad-market turning-point research dataset.

Examples
--------
Download long market/Cboe/FRED histories:
    python scripts/run_market_regime_research.py prepare

Audit FMP change events and publish PIT membership only if every gate passes:
    python scripts/run_market_regime_research.py pit

Build a market-only dataset while PIT equity history is still being repaired:
    python scripts/run_market_regime_research.py run --core-only

Build the full PIT breadth/momentum dataset:
    python scripts/run_market_regime_research.py run

Screen every P0 feature while keeping the 2022+ holdout sealed:
    python scripts/run_market_regime_research.py screen
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

# Direct script execution puts only scripts/ on sys.path.  Add the repository
# root before importing src.*, matching the other production entry points.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG, PROJECT_ROOT  # noqa: E402
from src.data.fmp import (
    get_historical_sp500_constituent_changes,
    get_sp500_constituents,
)  # noqa: E402
from src.market_regime_research.artifacts import write_strict_json  # noqa: E402
from src.market_regime_research.pipeline import (  # noqa: E402
    run_market_regime_research,
)
from src.market_regime_research.pit import (
    publish_validated_membership,
    reconstruct_with_settings,
)  # noqa: E402
from src.market_regime_research.settings import (
    MarketRegimeResearchSettings,
    load_market_regime_research_settings,
)  # noqa: E402
from src.market_regime_research.screening_pipeline import (  # noqa: E402
    run_effectiveness_screen,
)
from src.market_regime_research.sources import (  # noqa: E402
    prepare_market_sources,
    utc_now_iso,
)
from src.utils.identifiers import safe_path_component  # noqa: E402
from src.utils.market_calendar import latest_completed_xnys_session  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Auditable broad-market turning-point research pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser(
        "prepare",
        help="Download and validate long market, volatility, and credit histories",
    )
    prepare.add_argument("--force", action="store_true")
    prepare.add_argument(
        "--skip-credit",
        action="store_true",
        help="Skip FRED HY OAS only when diagnosing source connectivity",
    )

    pit = subparsers.add_parser(
        "pit",
        help="Reconstruct and audit complete SP500 membership snapshots",
    )
    pit.add_argument("--asof", help="Current-snapshot date; defaults to latest XNYS close")
    pit.add_argument(
        "--candidate-only",
        action="store_true",
        help="Never publish to data/pit_universes, even when all gates pass",
    )

    run = subparsers.add_parser(
        "run",
        help="Build labels and P0 features from prepared local data",
    )
    run.add_argument(
        "--core-only",
        action="store_true",
        help="Omit PIT breadth/momentum while retaining market/volatility/credit",
    )
    run.add_argument("--skip-credit", action="store_true")
    run.add_argument("--run-id")

    screen = subparsers.add_parser(
        "screen",
        help="Run leakage-controlled Stage B candidate effectiveness screening",
    )
    screen.add_argument(
        "--research-run-id",
        help="Immutable Stage A run; defaults to outputs/.../latest.json",
    )
    screen.add_argument("--screening-id")

    all_command = subparsers.add_parser(
        "all",
        help="Prepare sources, require a clean PIT build, then run full research",
    )
    all_command.add_argument("--force", action="store_true")
    all_command.add_argument("--skip-credit", action="store_true")
    all_command.add_argument("--run-id")
    return parser


def _print(value: dict[str, Any], *, error: bool = False) -> None:
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False),
        file=sys.stderr if error else sys.stdout,
    )


def _membership_target(settings: MarketRegimeResearchSettings) -> Path:
    configured = Path(str(CONFIG.universe.point_in_time.membership_dir))
    root = configured if configured.is_absolute() else PROJECT_ROOT / configured
    universe = safe_path_component(settings.pit.universe, label="PIT universe")
    return root / f"{universe}.parquet"


def _frame_sha256(frame: pd.DataFrame) -> str:
    """Hash the exact tabular payload used by a PIT reconstruction."""
    digest = hashlib.sha256()
    digest.update(
        json.dumps(
            {
                "columns": [str(column) for column in frame.columns],
                "dtypes": [str(dtype) for dtype in frame.dtypes],
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(
        pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes()
    )
    return f"sha256:{digest.hexdigest()}"


def _run_pit(
    settings: MarketRegimeResearchSettings,
    *,
    asof: str | None,
    candidate_only: bool,
) -> tuple[int, dict[str, Any]]:
    latest_session = latest_completed_xnys_session()
    asof_date = (
        pd.Timestamp(asof).tz_localize(None).normalize()
        if asof
        else latest_session
    )
    if asof_date != latest_session:
        raise ValueError(
            "--asof must equal the latest completed XNYS session "
            f"{latest_session.date()}; FMP supplies a current, not historical, "
            "constituent snapshot"
        )
    current = get_sp500_constituents()
    changes = get_historical_sp500_constituent_changes()

    # Always calculate the diagnostic candidate.  Production publication is a
    # separate gate below, so a failed build still leaves enough evidence to
    # repair provider events or symbol mappings.
    result = reconstruct_with_settings(
        current,
        changes,
        asof=asof_date,
        settings=settings.pit,
        strict=False,
    )
    staging_root = settings.raw_root / "pit"
    staging_root.mkdir(parents=True, exist_ok=True)
    candidate_path = staging_root / f"{settings.pit.universe}_candidate.parquet"
    events_path = staging_root / f"{settings.pit.universe}_events.parquet"
    diagnostics_path = staging_root / f"{settings.pit.universe}_diagnostics.json"
    result.membership.to_parquet(candidate_path, compression="snappy", index=False)
    events = result.normalized_events.copy()
    events["reason_codes"] = events["reason_codes"].map(
        lambda values: json.dumps(values, ensure_ascii=False)
    )
    events.to_parquet(events_path, compression="snappy", index=False)
    write_strict_json(diagnostics_path, result.diagnostics)

    payload: dict[str, Any] = {
        "quality_status": result.diagnostics["quality_status"],
        "candidate_path": str(candidate_path),
        "events_path": str(events_path),
        "diagnostics_path": str(diagnostics_path),
        "snapshots": result.diagnostics["snapshots"],
        "inconsistency_count": result.diagnostics["inconsistency_count"],
        "published": False,
    }
    if result.diagnostics["quality_status"] != "PASS":
        payload["error"] = (
            "PIT reconstruction failed closed; inspect diagnostics before "
            "repairing/provider-validating events."
        )
        return 2, payload
    if candidate_only:
        return 0, payload

    target = _membership_target(settings)
    strict_result = reconstruct_with_settings(
        current,
        changes,
        asof=asof_date,
        settings=settings.pit,
        strict=True,
    )
    membership_path, metadata_path = publish_validated_membership(
        strict_result,
        target,
        source_metadata={
            "provider": "FMP",
            "current_constituents_endpoint": "sp500-constituent",
            "historical_changes_endpoint": "historical-sp500-constituent",
            "current_rows": len(current),
            "change_rows": len(changes),
            "current_payload_sha256": _frame_sha256(current),
            "changes_payload_sha256": _frame_sha256(changes),
            "fetched_at": utc_now_iso(),
        },
    )
    payload["published"] = True
    payload["membership_path"] = str(membership_path)
    payload["metadata_path"] = str(metadata_path)
    return 0, payload


def _run_dataset(
    settings: MarketRegimeResearchSettings,
    *,
    core_only: bool,
    skip_credit: bool,
    run_id: str | None,
) -> dict[str, Any]:
    result = run_market_regime_research(
        settings,
        core_only=core_only,
        include_credit=not skip_credit,
        run_id=run_id,
    )
    return {
        "status": "SUCCESS",
        "run_id": result.run_id,
        "run_dir": str(result.run_dir),
        "features": str(result.features_path),
        "labels": str(result.labels_path),
        "feature_registry": str(result.feature_registry_path),
        "manifest": str(result.manifest_path),
        "diagnostics": str(result.diagnostics_path),
        "mode": "market_core_only" if core_only else "full_pit",
    }


def _run_screen(
    settings: MarketRegimeResearchSettings,
    *,
    research_run_id: str | None,
    screening_id: str | None,
) -> dict[str, Any]:
    result = run_effectiveness_screen(
        settings,
        research_run_id=research_run_id,
        screening_id=screening_id,
    )
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    return {
        "status": "SUCCESS",
        "screening_id": result.screening_id,
        "screening_dir": str(result.screening_dir),
        "source_research_run_id": summary["source_research_run_id"],
        "candidate_tests": summary["candidate_tests"],
        "stage_1_pass_count": summary["stage_1_pass_count"],
        "production_approved_count": 0,
        "holdout_status": summary["holdout_status"],
        "scorecard": str(result.scorecard_path),
        "report": str(result.report_path),
        "manifest": str(result.manifest_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    settings = load_market_regime_research_settings()
    try:
        if args.command == "prepare":
            manifest = prepare_market_sources(
                settings,
                force=bool(args.force),
                include_credit=not args.skip_credit,
            )
            _print(
                {
                    "status": "SUCCESS",
                    "source_manifest": str(settings.source_manifest_path),
                    "source_count": len(manifest["sources"]),
                    "credit_included": manifest["credit_included"],
                }
            )
            return 0

        if args.command == "pit":
            code, payload = _run_pit(
                settings,
                asof=args.asof,
                candidate_only=bool(args.candidate_only),
            )
            _print(payload, error=code != 0)
            return code

        if args.command == "run":
            _print(
                _run_dataset(
                    settings,
                    core_only=bool(args.core_only),
                    skip_credit=bool(args.skip_credit),
                    run_id=args.run_id,
                )
            )
            return 0

        if args.command == "screen":
            _print(
                _run_screen(
                    settings,
                    research_run_id=args.research_run_id,
                    screening_id=args.screening_id,
                )
            )
            return 0

        if args.command == "all":
            prepare_market_sources(
                settings,
                force=bool(args.force),
                include_credit=not args.skip_credit,
            )
            code, pit_payload = _run_pit(
                settings,
                asof=None,
                candidate_only=False,
            )
            if code != 0:
                _print(pit_payload, error=True)
                return code
            _print(
                _run_dataset(
                    settings,
                    core_only=False,
                    skip_credit=bool(args.skip_credit),
                    run_id=args.run_id,
                )
            )
            return 0
    except Exception as exc:  # noqa: BLE001 - stable CLI failure boundary
        _print(
            {
                "status": "FAILED",
                "error_type": type(exc).__name__,
                "message": str(exc),
            },
            error=True,
        )
        return 1
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
