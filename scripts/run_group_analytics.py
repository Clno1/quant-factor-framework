#!/usr/bin/env python3
"""Compute and atomically publish isolated group-analytics snapshots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.group_analytics.artifacts import normalize_json_value  # noqa: E402
from src.group_analytics.models import (  # noqa: E402
    GroupAnalyticsError,
    RunOutcome,
    RunRequest,
    RunStatus,
)
from src.group_analytics.service import GroupAnalyticsService  # noqa: E402
from src.group_analytics.settings import load_group_analytics_settings  # noqa: E402
from src.alerts.config import load_local_env  # noqa: E402


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_LEVELS = ("sector", "sub_industry")
_SENSITIVE_DETAIL_KEYS = (
    "authorization",
    "directory",
    "password",
    "path",
    "secret",
    "token",
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![:\w])/(?:[^\s,;]+)")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\s,;]+")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("eod", "live", "both"), default="eod")
    parser.add_argument("--universe", default="SP500")
    parser.add_argument("--taxonomy", default="FMP")
    parser.add_argument("--level", choices=(*_LEVELS, "all"), default="sector")
    parser.add_argument("--asof", default="latest")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--strict-pit", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output-run-id")
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Safely load KEY=VALUE settings without shell-sourcing the file.",
    )
    return parser


def _validate_cli(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.mode != "eod":
        parser.error("Stage 1 only supports --mode eod; live/both start in Stage 2")
    if args.history:
        parser.error("--history starts in Stage 3 and is unavailable in Stage 1")
    if args.start is not None or args.end is not None:
        parser.error("--start/--end may only be used with Stage-3 --history")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.output_run_id and not _RUN_ID_RE.fullmatch(args.output_run_id):
        parser.error(
            "--output-run-id must match ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
        )
    if args.level == "all" and args.output_run_id:
        parser.error(
            "--output-run-id cannot be shared by --level all; let each level "
            "receive its own generated run id"
        )


def _safe_value(value: object, *, key: str | None = None) -> object:
    """Keep CLI diagnostics useful without printing credentials or local paths."""
    normalized_key = (key or "").casefold()
    if any(marker in normalized_key for marker in _SENSITIVE_DETAIL_KEYS):
        return "<redacted>"
    if isinstance(value, Path):
        return "<redacted-path>"
    if isinstance(value, dict):
        return {
            str(item_key): _safe_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        redacted = _WINDOWS_ABSOLUTE_PATH_RE.sub("<redacted-path>", value)
        return _POSIX_ABSOLUTE_PATH_RE.sub("<redacted-path>", redacted)
    return value


def _expected_error(exc: GroupAnalyticsError) -> dict[str, object]:
    return {
        "code": exc.code,
        "stage": exc.stage,
        "message": _safe_value(str(exc)),
        "details": _safe_value(exc.details),
    }


def _unexpected_error(exc: Exception) -> dict[str, object]:
    # Deliberately omit ``str(exc)``: third-party exceptions commonly include
    # API URLs, filesystem roots, or credential-bearing request fragments.
    return {
        "code": "INTERNAL_ERROR",
        "stage": "cli",
        "message": "Unexpected group analytics failure; inspect protected service logs",
        "details": {"exception_type": type(exc).__name__},
    }


def _print_json(payload: object, *, stderr: bool = False) -> None:
    print(
        json.dumps(
            normalize_json_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ),
        file=sys.stderr if stderr else sys.stdout,
    )


def _request(args: argparse.Namespace, *, level: str) -> RunRequest:
    return RunRequest(
        universe=args.universe,
        taxonomy=args.taxonomy,
        level=level,
        mode=args.mode,
        asof=args.asof,
        dry_run=bool(args.dry_run),
        strict_pit=bool(args.strict_pit),
        force=bool(args.force),
        limit=args.limit,
        output_run_id=args.output_run_id,
    )


def _outcome_payload(outcome: RunOutcome) -> dict[str, object]:
    return {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "published": outcome.published,
        "dry_run": outcome.dry_run,
        "asof": outcome.asof,
        "artifact_locator": outcome.artifact_locator,
        "combination": outcome.combination.as_dict(),
        "error": _safe_value(outcome.error),
    }


def _run_level(
    service: GroupAnalyticsService,
    args: argparse.Namespace,
    *,
    level: str,
) -> tuple[dict[str, object], bool]:
    try:
        outcome = service.run(_request(args, level=level))
    except GroupAnalyticsError as exc:
        return {
            "level": level,
            "status": RunStatus.FAILED,
            "published": False,
            "error": _expected_error(exc),
        }, False
    except Exception as exc:  # noqa: BLE001 - keep the other level runnable
        return {
            "level": level,
            "status": RunStatus.FAILED,
            "published": False,
            "error": _unexpected_error(exc),
        }, False
    payload = _outcome_payload(outcome)
    succeeded = outcome.status in {RunStatus.SUCCESS, RunStatus.SKIPPED}
    return payload, succeeded


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _validate_cli(args, parser)
    try:
        if args.env_file is not None:
            load_local_env(args.env_file)
        settings = load_group_analytics_settings()
        service = GroupAnalyticsService(settings)
    except GroupAnalyticsError as exc:
        _print_json({"error": _expected_error(exc)}, stderr=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - service startup boundary
        _print_json({"error": _unexpected_error(exc)}, stderr=True)
        return 1

    if args.level != "all":
        payload, succeeded = _run_level(service, args, level=args.level)
        _print_json(payload, stderr="run_id" not in payload)
        return 0 if succeeded else 1

    results: list[dict[str, object]] = []
    succeeded_count = 0
    for level in _LEVELS:
        payload, succeeded = _run_level(service, args, level=level)
        results.append(payload)
        succeeded_count += int(succeeded)

    failed_count = len(results) - succeeded_count
    aggregate = {
        "status": RunStatus.SUCCESS if failed_count == 0 else RunStatus.FAILED,
        "requested_level": "all",
        "results": results,
        "summary": {
            "requested": len(results),
            "succeeded_or_skipped": succeeded_count,
            "failed": failed_count,
        },
    }
    _print_json(aggregate)
    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
