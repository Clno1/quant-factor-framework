#!/usr/bin/env python3
"""Build daily rotation profiles, then retry candidate association independently.

``--stage price`` publishes theme metrics without loading momentum.
``--stage linkage`` reuses the latest price snapshot and may advance ``latest.json``.
``--stage all`` (default) runs price then linkage for backward-compatible one-shot use.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alerts.config import load_local_env
from src.group_analytics.rotation.service import run_rotation
from src.group_analytics.rotation.store import RotationStore, encoded
from src.group_analytics.rotation.themes import Theme
from src.premarket_digest.rotation import attach_candidates

LINKAGE_PENDING_REASON = "个股关联待后续阶段"
SAFE_ERROR_CODE = re.compile(r"^[A-Z0-9_]{1,80}$")


class RotationStageError(ValueError):
    """CLI-stage failure with an explicit operations code."""

    def __init__(self, code, message, *, record_failure=True):
        super().__init__(message)
        self.code = code
        self.record_failure = record_failure


def load_momentum_report(source_session):
    """Import the momentum source only when the linkage stage actually runs."""
    from src.premarket_digest.momentum import CompletedSessionMomentumSource
    from src.premarket_digest.settings import load_premarket_digest_settings

    return CompletedSessionMomentumSource(
        load_premarket_digest_settings(load_env=False)
    ).load(source_session)


def safe_error_code(exc):
    code = getattr(exc, "code", "")
    return code if isinstance(code, str) and SAFE_ERROR_CODE.fullmatch(code) else type(exc).__name__


def resolve_source_session(asof):
    if asof == "latest":
        from src.group_analytics.calendar import latest_completed_session

        return latest_completed_session().date().isoformat()
    import pandas as pd

    return pd.Timestamp(asof).tz_localize(None).normalize().date().isoformat()


def failure_source_session(asof):
    try:
        return resolve_source_session(asof)
    except Exception:
        return asof


def same_price_inputs(existing, snapshot):
    return (
        existing.get("source_session") == snapshot.get("source_session")
        and existing.get("input_fingerprint") == snapshot.get("input_fingerprint")
        and existing.get("holdings_fingerprint") == snapshot.get("holdings_fingerprint")
        and existing.get("flows_fingerprint") == snapshot.get("flows_fingerprint")
    )


def _load_latest(store):
    if not (store.root / "latest.json").exists():
        return None
    return store.load()


def _summary(snapshot, *, status, run_id, stage, extra=None):
    payload = {
        "status": status,
        "stage": stage,
        "run_id": run_id,
        "source_session": snapshot.get("source_session") if snapshot else None,
        "valid_themes": snapshot.get("valid_theme_count") if snapshot else None,
        "total_themes": snapshot.get("total_theme_count") if snapshot else None,
        "candidate_linkage": (snapshot.get("candidate_linkage") or {}).get("status") if snapshot else None,
    }
    if extra:
        payload.update(extra)
    return payload


def run_price_stage(
    *,
    asof,
    refresh,
    store,
    dry_run,
    observations,
    decision_cutoff,
    amount_verified,
    themes,
    cache_root,
    without_candidates,
    holdings_root,
):
    snapshot = run_rotation(
        asof=asof, refresh=refresh, store=store, dry_run=True,
        observations=observations, decision_cutoff=decision_cutoff,
        amount_verified=amount_verified, themes=themes, cache_root=cache_root,
        holdings_root=holdings_root,
    )
    reason = "个股关联未启用" if without_candidates else LINKAGE_PENDING_REASON
    snapshot = attach_candidates(snapshot, None, unavailable_reason=reason)
    snapshot.pop("run_id", None)
    if dry_run:
        return _summary(snapshot, status="DRY_RUN", run_id=None, stage="price") | {"snapshot": snapshot}
    existing = _load_latest(store)
    if existing is not None and same_price_inputs(existing, snapshot):
        return _summary(existing, status="NOOP", run_id=existing.get("run_id"), stage="price") | {
            "snapshot": existing,
        }
    run_id = store.publish(snapshot)
    published = {**snapshot, "run_id": run_id}
    return _summary(published, status="SUCCESS", run_id=run_id, stage="price") | {"snapshot": published}


def run_linkage_stage(*, asof, store, dry_run, snapshot=None):
    expected = resolve_source_session(asof)
    if snapshot is None:
        try:
            snapshot = _load_latest(store)
        except FileNotFoundError:
            snapshot = None
        if snapshot is None:
            raise RotationStageError("NO_ROTATION_SNAPSHOT", "没有可关联的轮动价格快照")
    if snapshot.get("source_session") != expected:
        raise RotationStageError(
            "ROTATION_SESSION_MISMATCH",
            "最新快照的交易日与目标不一致，未覆盖现有快照",
            record_failure=False,
        )
    original_generated_at = snapshot.get("generated_at")
    original_run_id = snapshot.get("run_id")
    linkage = snapshot.get("candidate_linkage") or {}
    try:
        report = load_momentum_report(snapshot["source_session"])
    except Exception as exc:
        reason = f"同日动量扫描未通过数据门槛（{safe_error_code(exc)}），主题计算不受影响"
        if linkage.get("status") == "available":
            return _summary(snapshot, status="NOOP", run_id=original_run_id, stage="linkage") | {
                "snapshot": snapshot,
            }
        return _summary(
            snapshot, status="LINKAGE_UNAVAILABLE", run_id=original_run_id, stage="linkage",
            extra={"reason": reason},
        ) | {"snapshot": snapshot}
    new_fp = report.get("input_fingerprint") if isinstance(report, dict) else None
    if linkage.get("status") == "available" and linkage.get("input_fingerprint") == new_fp:
        return _summary(snapshot, status="NOOP", run_id=original_run_id, stage="linkage") | {
            "snapshot": snapshot,
        }
    working = dict(snapshot)
    working.pop("run_id", None)
    attached = attach_candidates(working, report)
    if attached["candidate_linkage"]["status"] != "available":
        reason = attached["candidate_linkage"].get("reason") or "没有同日突破扫描产物"
        return _summary(
            snapshot, status="LINKAGE_UNAVAILABLE", run_id=original_run_id, stage="linkage",
            extra={"reason": reason},
        ) | {"snapshot": snapshot}
    attached["generated_at"] = original_generated_at
    if dry_run:
        return _summary(attached, status="DRY_RUN", run_id=None, stage="linkage") | {"snapshot": attached}
    run_id = store.publish(attached)
    published = {**attached, "run_id": run_id}
    return _summary(published, status="SUCCESS", run_id=run_id, stage="linkage") | {"snapshot": published}


def run_all_stages(*, without_candidates, **price_kwargs):
    price = run_price_stage(without_candidates=without_candidates, **price_kwargs)
    if without_candidates:
        return price
    linkage_snapshot = price["snapshot"] if price_kwargs["dry_run"] else None
    linkage = run_linkage_stage(
        asof=price_kwargs["asof"], store=price_kwargs["store"],
        dry_run=price_kwargs["dry_run"], snapshot=linkage_snapshot,
    )
    combined = dict(linkage)
    combined["stage"] = "all"
    combined["price_status"] = price["status"]
    combined["linkage_status"] = linkage["status"]
    if linkage["status"] == "NOOP" and price["status"] not in {None, "NOOP"}:
        combined["status"] = price["status"]
    return combined


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default="latest")
    parser.add_argument("--stage", choices=("price", "linkage", "all"), default="all")
    parser.add_argument("--refresh", action="store_true", help="Refresh only registered symbols into group-owned cache")
    parser.add_argument("--dry-run", action="store_true", help="No publication or Discord delivery; --refresh may update price cache")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--holdings-root", type=Path,
                        help="Independent ETF holdings observation directory; missing data never blocks price")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--themes-file", type=Path, help="Optional versioned Theme records (JSON array)")
    parser.add_argument("--context-file", type=Path, help="Optional authorized as-of evidence records (JSON array)")
    parser.add_argument("--decision-cutoff", help="Timezone-aware evidence cutoff; default source-session close")
    parser.add_argument("--amount-verified", action=argparse.BooleanOptionalAction, default=True,
                        help="Unlock production amount after the canonical close×volume audit; --no-amount-verified disables it")
    parser.add_argument("--without-candidates", action="store_true")
    args = parser.parse_args(argv)
    if args.stage == "linkage" and args.refresh:
        print(json.dumps({"status": "FAILED", "error_type": "UsageError",
                          "message": "linkage 阶段不得重算价格，不要加 --refresh。"}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    if args.stage == "linkage" and args.without_candidates:
        print(json.dumps({"status": "FAILED", "error_type": "UsageError",
                          "message": "linkage 阶段必须尝试个股关联，不要加 --without-candidates。"}, ensure_ascii=False),
              file=sys.stderr)
        return 2
    store = RotationStore(args.output_root)
    try:
        if args.env_file:
            if load_local_env(args.env_file) is None:
                raise FileNotFoundError("Requested environment file is missing")
        from src.group_analytics.settings import load_group_analytics_settings
        if not load_group_analytics_settings().enabled:
            raise ValueError("Group analytics is disabled")
        observations = json.loads(args.context_file.read_text()) if args.context_file else []
        if not isinstance(observations, list):
            raise ValueError("context-file must contain an array")
        themes = None
        if args.themes_file:
            records = json.loads(args.themes_file.read_text())
            themes = [Theme(**{**r, "members": tuple(r.get("members", ()))}) for r in records]
            if not 1 <= len(themes) <= 100:
                raise ValueError("Expected 1-100 themes")
        price_kwargs = dict(
            asof=args.asof, refresh=args.refresh, store=store, dry_run=args.dry_run,
            observations=observations, decision_cutoff=args.decision_cutoff,
            amount_verified=args.amount_verified, themes=themes, cache_root=args.cache_root,
            holdings_root=args.holdings_root,
        )
        if args.stage == "price":
            result = run_price_stage(without_candidates=args.without_candidates, **price_kwargs)
        elif args.stage == "linkage":
            result = run_linkage_stage(asof=args.asof, store=store, dry_run=args.dry_run)
        else:
            result = run_all_stages(without_candidates=args.without_candidates, **price_kwargs)
        printable = {k: v for k, v in result.items() if k != "snapshot"}
        print(encoded(printable).decode())
        return 0
    except RotationStageError as exc:
        if not args.dry_run and exc.record_failure:
            store.failure(failure_source_session(args.asof), exc.code)
        print(json.dumps({"status": "FAILED", "error_type": exc.code,
                          "message": "轮动构建失败；检查缓存、日期和配置。未覆盖成功快照。"}, ensure_ascii=False),
              file=sys.stderr)
        return 1
    except Exception as exc:
        if not args.dry_run:
            store.failure(failure_source_session(args.asof), type(exc).__name__)
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__,
                          "message": "轮动构建失败；检查缓存、日期和配置。未覆盖成功快照。"}, ensure_ascii=False),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
