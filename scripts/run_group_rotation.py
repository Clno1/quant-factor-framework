#!/usr/bin/env python3
"""Build both daily rotation profiles and frozen candidate associations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.alerts.config import load_local_env
from src.group_analytics.rotation.service import run_rotation
from src.group_analytics.rotation.store import RotationStore, encoded
from src.group_analytics.rotation.themes import Theme
from src.premarket_digest.rotation import attach_candidates


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asof", default="latest")
    parser.add_argument("--refresh", action="store_true", help="Refresh only registered symbols into group-owned cache")
    parser.add_argument("--dry-run", action="store_true", help="No publication or Discord delivery; --refresh may update price cache")
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--cache-root", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--themes-file", type=Path, help="Optional versioned Theme records (JSON array)")
    parser.add_argument("--context-file", type=Path, help="Optional authorized as-of evidence records (JSON array)")
    parser.add_argument("--decision-cutoff", help="Timezone-aware evidence cutoff; default source-session close")
    parser.add_argument("--amount-verified", action="store_true", help="Only after provider price/volume adjustment audit")
    parser.add_argument("--without-candidates", action="store_true")
    args = parser.parse_args(argv)
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
        snapshot = run_rotation(asof=args.asof, refresh=args.refresh, store=store, dry_run=True,
                                observations=observations, decision_cutoff=args.decision_cutoff,
                                amount_verified=args.amount_verified, themes=themes, cache_root=args.cache_root)
        report, reason = None, "个股关联未启用"
        if not args.without_candidates:
            try:
                from src.premarket_digest.momentum import CompletedSessionMomentumSource
                from src.premarket_digest.settings import load_premarket_digest_settings
                report = CompletedSessionMomentumSource(load_premarket_digest_settings(load_env=False)).load(snapshot["source_session"])
            except Exception as exc:
                import re
                code = getattr(exc, "code", "")
                safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", code) else type(exc).__name__
                reason = f"同日动量扫描未通过数据门槛（{safe_code}），主题计算不受影响"
        snapshot = attach_candidates(snapshot, report, unavailable_reason=reason)
        snapshot.pop("run_id", None)
        run_id = None if args.dry_run else store.publish(snapshot)
        print(encoded({"status": "DRY_RUN" if args.dry_run else "SUCCESS", "run_id": run_id,
                       "source_session": snapshot["source_session"], "valid_themes": snapshot["valid_theme_count"],
                       "total_themes": snapshot["total_theme_count"], "candidate_linkage": snapshot["candidate_linkage"]["status"]}).decode())
        return 0
    except Exception as exc:
        if not args.dry_run:
            store.failure(args.asof, type(exc).__name__)
        print(json.dumps({"status": "FAILED", "error_type": type(exc).__name__,
                          "message": "轮动构建失败；检查缓存、日期和配置。未覆盖成功快照。"}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
