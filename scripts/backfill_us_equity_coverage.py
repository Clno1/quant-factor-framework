#!/usr/bin/env python3
"""Backfill broad US-equity coverage in resumable security batches.

The default is candidate-only.  ``--publish`` is accepted only for the full
approved Security Master selection and never for a limited pilot.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import resource
import sys
import time
from typing import Any
from uuid import uuid4

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.data.broad_coverage import (  # noqa: E402
    BAR_QUARANTINE_COLUMNS,
    BroadCoverageStore,
    coverage_bar_quarantine_checks,
    coverage_alias_intervals,
    normalize_coverage_bars,
    select_coverage_securities,
    split_coverage_bar_quality,
)
from src.data.fmp import get_canonical_historical_ohlcv  # noqa: E402
from src.data.price_semantics import (  # noqa: E402
    FMP_CANONICAL_SOURCE,
    build_price_semantics_contract,
)
from src.data.foundation import (  # noqa: E402
    MarketDataCatalog,
    MarketDataReader,
    QualityCheck,
)
from src.data.security_master_store import (  # noqa: E402
    SecurityMasterGeneration,
    SecurityMasterStore,
)
from src.data.universe_ids import US_EQUITY_COVERAGE  # noqa: E402
from src.utils.env import load_local_env  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402
from src.utils.market_calendar import latest_publishable_xnys_session  # noqa: E402


SCHEMA_VERSION = 1


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = CONFIG.data.broad_coverage
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-session")
    parser.add_argument("--history-start", default=str(settings.history_start))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ticker", action="append", default=[])
    parser.add_argument("--batch-size", type=int, default=int(settings.shard_size))
    parser.add_argument("--workers", type=int, default=int(settings.max_workers))
    parser.add_argument("--env-file")
    parser.add_argument("--security-master-candidate-dir")
    parser.add_argument("--resume-run-dir")
    parser.add_argument(
        "--auto-resume",
        action="store_true",
        help="resume the only checkpoint matching every immutable input",
    )
    parser.add_argument("--output-dir", default=str(settings.candidate_dir))
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


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    columns = sorted(str(column) for column in frame.columns)
    ordered = frame.loc[:, columns].astype(str).sort_values(columns).reset_index(drop=True)
    digest = hashlib.sha256("\x1f".join(columns).encode("utf-8"))
    for offset in range(0, len(ordered), 5_000):
        digest.update(
            pd.util.hash_pandas_object(
                ordered.iloc[offset:offset + 5_000],
                index=False,
            ).to_numpy().tobytes()
        )
    return digest.hexdigest()


def _auto_resume_run_dir(
    output_dir: Path,
    *,
    expected: dict[str, Any],
) -> tuple[Path | None, list[dict[str, Any]]]:
    """Return one exact RUNNING checkpoint; reject ambiguous matches."""
    root = output_dir / f"asof={expected['target_session']}"
    matches: list[Path] = []
    diagnostics: list[dict[str, Any]] = []
    for checkpoint_path in sorted(root.glob("run=*/checkpoint.json")):
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            diagnostics.append({
                "run_dir": str(checkpoint_path.parent),
                "decision": "REJECT_MALFORMED",
            })
            continue
        mismatches = [
            key for key, value in expected.items()
            if checkpoint.get(key) != value
        ]
        status = str(checkpoint.get("status") or "")
        if status not in {"RUNNING", "FAIL"}:
            mismatches.append("status")
        decision = "MATCH" if not mismatches else "REJECT"
        diagnostics.append({
            "run_dir": str(checkpoint_path.parent),
            "decision": decision,
            "status": status,
            "mismatches": sorted(set(mismatches)),
        })
        if not mismatches:
            matches.append(checkpoint_path.parent.resolve())
    if len(matches) > 1:
        raise RuntimeError(
            "multiple exact coverage checkpoints match current inputs: "
            + ", ".join(str(path) for path in matches)
        )
    return (matches[0] if matches else None), diagnostics


def _published_security_master() -> tuple[
    SecurityMasterGeneration, dict[str, pd.DataFrame], bool
]:
    settings = CONFIG.data.security_master
    store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(settings.snapshot_dir)),
    )
    generation, frames = store.load_published()
    return generation, frames, True


def _candidate_security_master(
    directory: Path,
) -> tuple[SecurityMasterGeneration, dict[str, pd.DataFrame], bool]:
    root = directory.resolve()
    report_path = root / "audit.json"
    if not report_path.is_file():
        raise FileNotFoundError(f"Security Master audit not found: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if (report.get("quality") or {}).get("status") != "PASS":
        raise RuntimeError("Security Master candidate did not pass its quality gates")
    frames: dict[str, pd.DataFrame] = {}
    for name in (
        "master", "symbols", "classifications", "identity_keys",
        "history_policy",
    ):
        artifact = (report.get("candidate_artifacts") or {}).get(name) or {}
        path = Path(str(artifact.get("path") or root / f"{name}.parquet"))
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file() or _sha256(path) != artifact.get("sha256"):
            raise RuntimeError(f"Security Master candidate {name} hash mismatch")
        frames[name] = pd.read_parquet(path)
    target = pd.Timestamp(report["target_session"]).date()
    generation = SecurityMasterGeneration(
        generation_id=f"candidate_{_sha256(report_path)[:24]}",
        target_session=target,
        created_at=datetime.fromisoformat(str(report["generated_at"])),
        status="CANDIDATE",
        row_count=len(frames["master"]),
        active_count=int(frames["master"]["trading_status"].eq("ACTIVE").sum()),
        master_path=str((root / "master.parquet").resolve()),
        symbols_path=str((root / "symbols.parquet").resolve()),
        classifications_path=str((root / "classifications.parquet").resolve()),
        identity_keys_path=str((root / "identity_keys.parquet").resolve()),
        manifest_path=str(report_path),
        master_sha256=(report["candidate_artifacts"]["master"]["sha256"]),
        symbols_sha256=(report["candidate_artifacts"]["symbols"]["sha256"]),
        classifications_sha256=(
            report["candidate_artifacts"]["classifications"]["sha256"]
        ),
        identity_keys_sha256=(
            report["candidate_artifacts"]["identity_keys"]["sha256"]
        ),
        manifest_sha256=_sha256(report_path),
    )
    return generation, frames, False


def _choose_pilot(
    universe: pd.DataFrame,
    symbols: pd.DataFrame,
    *,
    limit: int | None,
    required_tickers: list[str],
) -> pd.DataFrame:
    if limit is None:
        return universe.copy()
    if limit < 1:
        raise ValueError("limit must be positive")
    required = {
        str(value).strip().upper().replace(".", "-").replace("/", "-")
        for value in required_tickers
    }
    required.update({"MDB", "AEVA"})
    selected_ids = set(
        universe.loc[universe["ticker"].isin(required), "security_id"].astype(str)
    )
    # Include one dated rename and one confirmed exit when available so a
    # 100-security pilot exercises identity continuity and survivorship.
    alias_counts = symbols.groupby("security_id")["ticker"].nunique()
    renamed = [value for value in alias_counts[alias_counts.gt(1)].index if value in set(universe["security_id"])]
    if renamed:
        selected_ids.add(str(sorted(renamed)[0]))
    exited = universe.loc[universe["delisting_date"].notna(), "security_id"].astype(str)
    if not exited.empty:
        selected_ids.add(sorted(exited)[0])
    ordered = universe.assign(
        _required=universe["security_id"].astype(str).isin(selected_ids)
    ).sort_values(["_required", "is_current_coverage", "security_id"], ascending=[False, False, True])
    return ordered.head(limit).drop(columns="_required").reset_index(drop=True)


def _fetch_security(
    security_id: str,
    aliases: pd.DataFrame,
    current_ticker: str,
) -> tuple[pd.DataFrame | None, list[dict[str, Any]], list[dict[str, Any]]]:
    pieces: list[pd.DataFrame] = []
    failures: list[dict[str, Any]] = []
    fallbacks: list[dict[str, Any]] = []
    for row in aliases.itertuples(index=False):
        start = pd.Timestamp(row.fetch_start).date().isoformat()
        end = pd.Timestamp(row.fetch_end).date().isoformat()
        frame = get_canonical_historical_ohlcv(str(row.ticker), start, end)
        if frame is None or frame.empty:
            fallback = str(current_ticker).strip().upper()
            if fallback and fallback != str(row.ticker):
                frame = get_canonical_historical_ohlcv(fallback, start, end)
                if frame is not None and not frame.empty:
                    fallbacks.append({
                        "requested_ticker": str(row.ticker),
                        "provider_ticker": fallback,
                        "start": start,
                        "end": end,
                        "rows": len(frame),
                    })
            if frame is None or frame.empty:
                failures.append({"ticker": str(row.ticker), "start": start, "end": end})
                continue
        work = frame.reset_index()
        work["security_id"] = security_id
        work["ticker"] = str(row.ticker)
        pieces.append(work)
    if not pieces:
        return None, failures, fallbacks
    combined = pd.concat(pieces, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"]).dt.normalize()
    duplicates = combined.loc[
        combined.duplicated(["date", "security_id"], keep=False)
    ].copy()
    if not duplicates.empty:
        numeric = ["open", "high", "low", "close", "adj_close", "volume"]
        conflicts = duplicates.groupby(["date", "security_id"])[numeric].nunique().max(axis=1).gt(1)
        if conflicts.any():
            raise RuntimeError(
                f"{security_id} aliases returned conflicting bars on "
                f"{int(conflicts.sum())} dates"
            )
    combined = combined.drop_duplicates(["date", "security_id"], keep="last")
    return combined.sort_values("date").reset_index(drop=True), failures, fallbacks


def _write_batch(
    frame: pd.DataFrame,
    *,
    run_dir: Path,
    batch_index: int,
    target: pd.Timestamp,
    run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized = normalize_coverage_bars(
        frame,
        target_session=target,
        ingestion_run_id=run_id,
    )
    normalized, quarantine = split_coverage_bar_quality(normalized)
    normalized["_year"] = normalized["date"].dt.year
    artifacts: list[dict[str, Any]] = []
    for year, rows in normalized.groupby("_year", sort=True):
        path = run_dir / "partitions" / f"batch={batch_index:05d}" / f"year={int(year)}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        rows.drop(columns="_year").to_parquet(path, index=False)
        artifacts.append({
            "path": str(path.resolve()),
            "sha256": _sha256(path),
            "rows": len(rows),
            "year": int(year),
        })
    quarantine["_year"] = pd.to_datetime(quarantine["date"]).dt.year
    quarantine_artifacts: list[dict[str, Any]] = []
    for year, rows in quarantine.groupby("_year", sort=True):
        path = (
            run_dir / "quarantine" / f"batch={batch_index:05d}"
            / f"year={int(year)}.parquet"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        rows.drop(columns="_year").to_parquet(path, index=False)
        quarantine_artifacts.append({
            "path": str(path.resolve()),
            "sha256": _sha256(path),
            "rows": len(rows),
            "year": int(year),
        })
    return artifacts, quarantine_artifacts


def _run_directory(output_dir: Path, target: pd.Timestamp) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return output_dir / f"asof={target.date()}" / f"run={stamp}_{uuid4().hex[:8]}"


def _prepare_resumed_checkpoint(
    checkpoint: dict[str, Any],
    *,
    expected: dict[str, Any],
    total_batches: int,
    resume_diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate an immutable checkpoint and restore observable progress."""
    checkpoint.setdefault("alias_failures", [])
    checkpoint.setdefault("alias_fallbacks", [])
    mismatches = [
        key for key, value in expected.items()
        if checkpoint.get(key) != value
    ]
    if mismatches:
        raise RuntimeError(f"resume checkpoint mismatch: {mismatches}")
    recorded = checkpoint.get("batches") or {}
    checkpoint["status"] = "RUNNING"
    checkpoint["resume_diagnostics"] = resume_diagnostics
    checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
    checkpoint["current_phase"] = "FETCHING"
    checkpoint["progress"] = {
        "completed_batches": len(recorded),
        "total_batches": int(total_batches),
        "successful_batches": sum(
            value.get("status") == "SUCCESS" for value in recorded.values()
        ),
        "partial_batches": sum(
            value.get("status") == "PARTIAL" for value in recorded.values()
        ),
    }
    return checkpoint


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    target = (
        pd.Timestamp(args.target_session).normalize()
        if args.target_session
        else pd.Timestamp(latest_publishable_xnys_session()).normalize()
    )
    history_start = pd.Timestamp(args.history_start).normalize()
    if history_start > target:
        raise ValueError("history-start is after target-session")
    if args.publish and (args.limit is not None or args.security_master_candidate_dir):
        raise ValueError("formal publication requires the full published Security Master")
    if args.auto_resume and args.resume_run_dir:
        raise ValueError("--auto-resume and --resume-run-dir are mutually exclusive")
    generation, frames, formal_security_master = (
        _candidate_security_master(Path(args.security_master_candidate_dir))
        if args.security_master_candidate_dir
        else _published_security_master()
    )
    if target.date() > generation.target_session:
        raise RuntimeError(
            "target session exceeds Security Master generation: "
            f"{target.date()} > {generation.target_session}"
        )
    catalog = MarketDataCatalog(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path))
    )
    published = catalog.latest_version(US_EQUITY_COVERAGE)
    if (
        args.publish
        and published is not None
        and published.target_session == target.date()
    ):
        manifest = MarketDataReader(catalog=catalog).verify_version(
            published,
            require_price_semantics=True,
        )
        if (
            manifest.get("security_master_generation_id")
            == generation.generation_id
            and manifest.get("security_master_manifest_sha256")
            == generation.manifest_sha256
        ):
            return {
                "status": "NOOP",
                "target_session": target.date().isoformat(),
                "selected_security_count": int(published.ticker_count),
                "bar_rows": int(published.row_count),
                "partition_count": int(manifest.get("partition_count") or 0),
                "missing_security_count": 0,
                "publication": published.to_dict(),
                "run_dir": None,
                "resumed": False,
                "resume_diagnostics": [],
                "report_path": str(published.manifest_path),
                "report_sha256": published.manifest_checksum_sha256,
                "duration_seconds": round(time.perf_counter() - started, 3),
                "peak_rss_mb": round(_rss_mb(), 3),
            }, 0
    settings = CONFIG.data.broad_coverage
    universe = select_coverage_securities(
        frames["master"],
        history_start=history_start,
        target_session=target,
        allowed_asset_types=list(settings.allowed_asset_types),
        benchmark_tickers=list(settings.benchmark_tickers),
        history_policy=frames.get("history_policy"),
    )
    universe = _choose_pilot(
        universe,
        frames["symbols"],
        limit=args.limit,
        required_tickers=list(args.ticker),
    )
    aliases = coverage_alias_intervals(
        universe,
        frames["symbols"],
        history_start=history_start,
        target_session=target,
    )
    checkpoint_identity = {
        "target_session": target.date().isoformat(),
        "history_start": history_start.date().isoformat(),
        "security_master_generation_id": generation.generation_id,
        "security_master_manifest_sha256": generation.manifest_sha256,
        "security_master_is_formal": formal_security_master,
        "selected_security_count": len(universe),
        "batch_size": int(args.batch_size),
        "methodology_version": str(settings.methodology_version),
        "universe_content_sha256": _frame_fingerprint(universe),
        "aliases_content_sha256": _frame_fingerprint(aliases),
    }
    resume_diagnostics: list[dict[str, Any]] = []
    auto_resume_dir = None
    if args.auto_resume:
        auto_resume_dir, resume_diagnostics = _auto_resume_run_dir(
            Path(args.output_dir).resolve(),
            expected=checkpoint_identity,
        )
    run_dir = (
        Path(args.resume_run_dir).resolve()
        if args.resume_run_dir
        else auto_resume_dir
        if auto_resume_dir is not None
        else _run_directory(Path(args.output_dir).resolve(), target)
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = run_dir / "checkpoint.json"
    run_id = run_dir.name.split("run=", 1)[-1]
    security_ids = universe["security_id"].astype(str).tolist()
    batches = [
        security_ids[index:index + int(args.batch_size)]
        for index in range(0, len(security_ids), int(args.batch_size))
    ]
    if checkpoint_path.exists():
        checkpoint = _prepare_resumed_checkpoint(
            json.loads(checkpoint_path.read_text(encoding="utf-8")),
            expected=checkpoint_identity,
            total_batches=len(batches),
            resume_diagnostics=resume_diagnostics,
        )
        atomic_save_json(checkpoint, checkpoint_path)
    else:
        universe_path = run_dir / "security_universe.parquet"
        aliases_path = run_dir / "alias_intervals.parquet"
        universe.to_parquet(universe_path, index=False)
        aliases.to_parquet(aliases_path, index=False)
        checkpoint = {
            "schema_version": SCHEMA_VERSION,
            "status": "RUNNING",
            "run_id": run_id,
            "target_session": target.date().isoformat(),
            "history_start": history_start.date().isoformat(),
            "security_master_generation_id": generation.generation_id,
            "security_master_manifest_sha256": generation.manifest_sha256,
            "security_master_is_formal": formal_security_master,
            "selected_security_count": len(universe),
            "batch_size": int(args.batch_size),
            "methodology_version": str(settings.methodology_version),
            "universe_content_sha256": checkpoint_identity[
                "universe_content_sha256"
            ],
            "aliases_content_sha256": checkpoint_identity[
                "aliases_content_sha256"
            ],
            "universe_path": str(universe_path.resolve()),
            "universe_sha256": _sha256(universe_path),
            "aliases_path": str(aliases_path.resolve()),
            "aliases_sha256": _sha256(aliases_path),
            "batches": {},
            "alias_failures": [],
            "alias_fallbacks": [],
            "resume_diagnostics": resume_diagnostics,
        }
        atomic_save_json(checkpoint, checkpoint_path)

    for batch_index, batch_ids in enumerate(batches):
        key = str(batch_index)
        existing = (checkpoint.get("batches") or {}).get(key)
        if existing and existing.get("status") == "SUCCESS":
            if all(
                Path(item["path"]).is_file()
                and _sha256(Path(item["path"])) == item["sha256"]
                for item in existing.get("artifacts", [])
            ):
                continue
            raise RuntimeError(f"completed batch {batch_index} failed hash verification")
        frames_by_security: list[pd.DataFrame] = []
        batch_failures: list[dict[str, Any]] = []
        batch_fallbacks: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as executor:
            futures = {
                executor.submit(
                    _fetch_security,
                    security_id,
                    aliases.loc[aliases["security_id"].astype(str).eq(security_id)],
                    str(
                        universe.loc[
                            universe["security_id"].astype(str).eq(security_id),
                            "current_ticker",
                        ].iloc[0]
                    ),
                ): security_id
                for security_id in batch_ids
            }
            for future in as_completed(futures):
                security_id = futures[future]
                try:
                    frame, failures, fallbacks = future.result()
                except Exception as exc:  # noqa: BLE001
                    frame = None
                    failures = [{"error": str(exc)}]
                    fallbacks = []
                if frame is not None and not frame.empty:
                    frames_by_security.append(frame)
                else:
                    batch_failures.append({
                        "security_id": security_id,
                        "reason": "NO_BARS",
                    })
                batch_failures.extend(
                    {"security_id": security_id, **failure}
                    for failure in failures
                )
                batch_fallbacks.extend(
                    {"security_id": security_id, **fallback}
                    for fallback in fallbacks
                )
        if not frames_by_security:
            raise RuntimeError(f"batch {batch_index} returned no bars")
        artifacts, quarantine_artifacts = _write_batch(
            pd.concat(frames_by_security, ignore_index=True),
            run_dir=run_dir,
            batch_index=batch_index,
            target=target,
            run_id=run_id,
        )
        checkpoint["batches"][key] = {
            "status": "SUCCESS" if not batch_failures else "PARTIAL",
            "security_count": len(frames_by_security),
            "requested_security_count": len(batch_ids),
            "artifacts": artifacts,
            "quarantine_artifacts": quarantine_artifacts,
            "failures": batch_failures,
            "fallbacks": batch_fallbacks,
        }
        checkpoint["alias_failures"] = [
            failure
            for batch in checkpoint["batches"].values()
            for failure in batch.get("failures") or []
        ]
        checkpoint["alias_fallbacks"] = [
            fallback
            for batch in checkpoint["batches"].values()
            for fallback in batch.get("fallbacks") or []
        ]
        checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_save_json(checkpoint, checkpoint_path)
        print(
            f"[coverage] batch {batch_index + 1}/{len(batches)} "
            f"securities={len(frames_by_security)}/{len(batch_ids)} "
            f"failures={len(batch_failures)}",
            file=sys.stderr,
            flush=True,
        )

    partition_paths = [
        Path(item["path"])
        for batch in checkpoint["batches"].values()
        for item in batch["artifacts"]
    ]
    quarantine_partition_paths = [
        Path(item["path"])
        for batch in checkpoint["batches"].values()
        for item in batch.get("quarantine_artifacts") or []
    ]
    if checkpoint["alias_failures"]:
        failed_ids = sorted({
            str(item.get("security_id") or "")
            for item in checkpoint["alias_failures"]
            if item.get("security_id")
        })
        alias_check = QualityCheck(
            "alias_interval_coverage",
            False,
            {
                "failure_count": len(checkpoint["alias_failures"]),
                "failed_security_count": len(failed_ids),
                "failed_security_sample": failed_ids[:20],
                "failure_sample": checkpoint["alias_failures"][:20],
            },
            {"failure_count": 0},
            "all dated ticker intervals have provider bars",
        )
        bar_rows = sum(
            int(item.get("rows") or 0)
            for batch in checkpoint["batches"].values()
            for item in batch.get("artifacts") or []
        )
        checkpoint.update({
            "status": "FAIL",
            "current_phase": "FETCH_FAILED",
            "failure_stage": "ALIAS_INTERVAL_COVERAGE",
            "quality_checks": [alias_check.to_dict()],
            "statistics": {
                "staged_row_count": bar_rows,
                "partition_count": len(partition_paths),
                "failed_security_count": len(failed_ids),
                "validation_skipped": True,
                "validation_skip_reason": "provider or alias failures",
            },
            "publication": None,
            "duration_seconds": round(time.perf_counter() - started, 3),
            "peak_rss_mb": round(_rss_mb(), 3),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        atomic_save_json(checkpoint, checkpoint_path)
        report_path = run_dir / "audit.json"
        atomic_save_json(checkpoint, report_path)
        return {
            "status": "FAIL",
            "failure_stage": "ALIAS_INTERVAL_COVERAGE",
            "target_session": checkpoint["target_session"],
            "selected_security_count": len(universe),
            "bar_rows": bar_rows,
            "partition_count": len(partition_paths),
            "missing_security_count": len(failed_ids),
            "publication": None,
            "run_dir": str(run_dir),
            "resumed": bool(args.resume_run_dir or auto_resume_dir is not None),
            "resume_diagnostics": resume_diagnostics,
            "report_path": str(report_path),
            "report_sha256": _sha256(report_path),
            "duration_seconds": checkpoint["duration_seconds"],
            "peak_rss_mb": checkpoint["peak_rss_mb"],
        }, 2

    checkpoint["current_phase"] = "VALIDATING"
    checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save_json(checkpoint, checkpoint_path)
    observed_ids: set[str] = set()
    for path in partition_paths:
        observed_ids.update(
            pd.read_parquet(path, columns=["security_id"])["security_id"].astype(str)
        )
    missing_ids = sorted(set(security_ids) - observed_ids)
    presence_check = QualityCheck(
        "selected_security_bar_presence",
        not missing_ids,
        {"selected": len(security_ids), "observed": len(observed_ids), "missing_sample": missing_ids[:20]},
        {"missing": 0},
        "every selected coverage security has at least one historical bar",
    )
    alias_check = QualityCheck(
        "alias_interval_coverage",
        not checkpoint["alias_failures"],
        {
            "failure_count": len(checkpoint["alias_failures"]),
            "failure_sample": checkpoint["alias_failures"][:20],
            "verified_lineage_fallback_count": len(checkpoint["alias_fallbacks"]),
        },
        {"failure_count": 0},
        "all dated ticker intervals have provider bars",
    )
    quarantine_frames = [
        pd.read_parquet(path) for path in quarantine_partition_paths
    ]
    quarantine = (
        pd.concat(quarantine_frames, ignore_index=True)
        if quarantine_frames
        else pd.DataFrame(columns=BAR_QUARANTINE_COLUMNS)
    )
    quarantine = quarantine.loc[:, BAR_QUARANTINE_COLUMNS].sort_values(
        ["date", "security_id", "quality_reasons"]
    ).reset_index(drop=True)
    quarantine_path = run_dir / "bar_quarantine.parquet"
    quarantine.to_parquet(quarantine_path, index=False, compression="snappy")
    accepted_rows = sum(
        int(item.get("rows") or 0)
        for batch in checkpoint["batches"].values()
        for item in batch.get("artifacts") or []
    )
    source_rows = accepted_rows + len(quarantine)
    quarantine_checks = coverage_bar_quarantine_checks(
        quarantine,
        source_row_count=source_rows,
        security_universe=universe,
        target_session=target,
        max_ratio=float(settings.max_bar_quarantine_ratio),
        max_target_ratio=float(settings.max_target_bar_quarantine_ratio),
    )
    store = BroadCoverageStore(
        catalog=catalog,
        lake_dir=CONFIG.abs_path(str(CONFIG.data.foundation.lake_dir)),
    )
    checks, stats = store._validate_partitions(
        partition_paths,
        security_universe=universe,
        target_session=target,
        min_target_coverage=float(settings.min_target_coverage),
    )
    checks.extend([presence_check, alias_check, *quarantine_checks])
    passed = all(check.passed for check in checks)
    publication = None
    if args.publish and passed:
        publication = store.publish_partitions(
            partition_paths,
            security_universe=universe,
            target_session=target,
            security_master=generation,
            price_semantics=build_price_semantics_contract(
                source=FMP_CANONICAL_SOURCE,
                history_mode="FULL_REBUILD",
            ),
            min_target_coverage=float(settings.min_target_coverage),
            external_checks=[presence_check, alias_check, *quarantine_checks],
            run_id=run_id,
            bar_quarantine_path=quarantine_path,
            quality_lineage={
                "policy": "PROVIDER_BAD_BAR_QUARANTINE_V1",
                "source_row_count": source_rows,
                "accepted_row_count": accepted_rows,
                "quarantined_row_count": len(quarantine),
                "quarantine_sha256": _sha256(quarantine_path),
            },
        )
    checkpoint["status"] = "PUBLISHED" if publication else "PASS" if passed else "FAIL"
    checkpoint["current_phase"] = (
        "PUBLISHED" if publication else "COMPLETE" if passed else "VALIDATION_FAILED"
    )
    checkpoint["quality_checks"] = [check.to_dict() for check in checks]
    checkpoint["statistics"] = stats
    checkpoint["statistics"].update({
        "provider_source_row_count": source_rows,
        "accepted_row_count": accepted_rows,
        "quarantined_row_count": len(quarantine),
        "bar_quarantine_path": str(quarantine_path),
        "bar_quarantine_sha256": _sha256(quarantine_path),
    })
    checkpoint["publication"] = (
        publication.version.to_dict() if publication is not None else None
    )
    checkpoint["duration_seconds"] = round(time.perf_counter() - started, 3)
    checkpoint["peak_rss_mb"] = round(_rss_mb(), 3)
    checkpoint["finished_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save_json(checkpoint, checkpoint_path)
    report_path = run_dir / "audit.json"
    atomic_save_json(checkpoint, report_path)
    report_sha = _sha256(report_path)
    return {
        "status": checkpoint["status"],
        "target_session": checkpoint["target_session"],
        "selected_security_count": len(universe),
        "bar_rows": stats["row_count"],
        "partition_count": len(partition_paths),
        "missing_security_count": len(missing_ids),
        "publication": checkpoint["publication"],
        "run_dir": str(run_dir),
        "resumed": bool(args.resume_run_dir or auto_resume_dir is not None),
        "resume_diagnostics": resume_diagnostics,
        "report_path": str(report_path),
        "report_sha256": report_sha,
        "duration_seconds": checkpoint["duration_seconds"],
        "peak_rss_mb": checkpoint["peak_rss_mb"],
    }, 0 if passed else 2


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_local_env(args.env_file)
    result, exit_code = run(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            "status={status} target={target_session} securities={selected_security_count} "
            "rows={bar_rows} partitions={partition_count} report={report_path}".format(
                **result
            )
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
