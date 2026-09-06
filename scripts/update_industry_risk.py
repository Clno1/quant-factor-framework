#!/usr/bin/env python3
"""Explicit provider writer for current industry risk observations and reports.

No account run, orders, factor publication, or historical classification writes.
An exported SG account snapshot can be used without opening the local app DB.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.data.fmp import _request, get_security_profile
from src.risk.industry import TAXONOMY, benchmark_for, build_industry_report, snapshot_root, load_snapshot
from src.utils.env import load_local_env
from src.utils.io import atomic_save_json


def collect_snapshot(tickers: list[str], benchmarks: list[str]) -> dict:
    observed = datetime.now(timezone.utc).isoformat()
    classifications, benchmark_data, errors = {}, {}, []
    for ticker in sorted(set(tickers)):
        try:
            profile = get_security_profile(ticker)
            if not profile:
                raise ValueError("No profile returned")
            classifications[ticker] = {
                key: profile.get(key) for key in
                ("ticker", "name", "sector", "sub_industry", "isin", "cusip", "cik", "asset_type")
            }
        except Exception as exc:
            errors.append({"symbol": ticker, "stage": "profile", "error_type": type(exc).__name__})
    for symbol in sorted(set(benchmarks)):
        try:
            payload = _request("/etf/sector-weightings", {"symbol": symbol}, retry=0).json()
            if not isinstance(payload, list) or not payload:
                raise ValueError("Empty ETF sector weights")
            weights = {}
            for row in payload:
                if row.get("symbol") != symbol:
                    raise ValueError("ETF symbol mismatch")
                sector = str(row["sector"]).strip()
                if sector in weights:
                    raise ValueError("Duplicate ETF sector")
                # FMP explicitly reports percentage points, not fractions.
                weights[sector] = float(row["weightPercentage"]) / 100.0
            dates = {str(row["date"]) for row in payload if row.get("date")}
            if len(dates) > 1:
                raise ValueError("Mixed ETF sector dates")
            benchmark_data[symbol] = {
                "taxonomy": TAXONOMY, "source": "FMP /stable/etf/sector-weightings",
                "observed_at": observed, "asof": next(iter(dates), None),
                "date_basis": "PROVIDER_DATE" if dates else "OBSERVATION_ONLY_NO_PROVIDER_DATE",
                "weights": weights, "raw": payload,
            }
        except Exception as exc:
            errors.append({"symbol": symbol, "stage": "benchmark", "error_type": type(exc).__name__})
    return {
        "schema_version": 1, "observed_at": observed, "taxonomy": TAXONOMY,
        "classification_policy": "CURRENT_OBSERVATION_ONLY",
        "classification_source": "FMP /stable/profile",
        "classifications": classifications, "benchmarks": benchmark_data, "errors": errors,
    }


def publish_snapshot(snapshot: dict, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2).encode()
    digest = hashlib.sha256(raw).hexdigest()
    name = f"observation_{digest}.json"
    path = root / name
    if not path.exists():
        # Content-addressed immutable data, pointer is replaced only after fs close.
        with path.open("xb") as stream:
            stream.write(raw)
    atomic_save_json({"file": name, "sha256": digest}, root / "latest.json")
    return path


def write_report(report: dict, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    aid = str(report["account_id"])
    from src.utils.identifiers import canonical_uuid
    aid = canonical_uuid(aid, label="account_id")
    atomic_save_json(report, output / f"{aid}.json")
    pd.DataFrame(report["rows"]).to_csv(output / f"{aid}.csv", index=False)
    lines = [
        f"# {report['account_name']} · 行业风险报告", "",
        f"状态：{report['status']}；持仓估值日：{report['valuation_date']}；基准：{report['benchmark'] or '未指定'}。",
        f"分类观测时间：{report['classification_observed_at']}。",
        f"基准观测时间：{report['benchmark_observed_at']}；供应商权重日期：{report['benchmark_asof'] or '未提供'}。",
        "", "行业标签和基准权重为当前观测；不用于历史时点归因或历史中性化。",
        "", "| 行业 | 股票数 | 净资产权重 | 股票资产内权重 | 基准权重 | 偏离（百分点） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    def pct(value):
        return "—" if value is None else f"{value * 100:.2f}%"
    for row in report["rows"]:
        active = "—" if row["active_weight_pp"] is None else f"{row['active_weight_pp']:+.2f}"
        lines.append(f"| {row['sector']} | {row['count']} | {pct(row['nav_weight'])} | {pct(row['equity_weight'])} | {pct(row['benchmark_weight'])} | {active} |")
    lines.extend(["", f"数据检查：{', '.join(report['issues']) or '通过'}", ""])
    (output / f"{aid}.md").write_text("\n".join(lines))


def refresh_current_reports() -> list[dict]:
    """Refresh after the existing paper CLI run; reuse complete same-day data."""
    from src.papertrading.store import list_accounts, load_account, load_table
    accounts = [{"account": load_account(a["id"]), "positions": load_table(a["id"], "positions")} for a in list_accounts()]
    if not accounts:
        return []
    tickers = {str(t) for a in accounts for t in a["positions"].get("ticker", [])}
    benchmarks = {benchmark_for(a["account"]) for a in accounts} - {None}
    snapshot = load_snapshot()
    today = datetime.now(timezone.utc).date()
    reusable = bool(snapshot.get("observed_at")) and pd.Timestamp(snapshot["observed_at"]).date() == today and tickers.issubset(snapshot.get("classifications", {})) and benchmarks.issubset(snapshot.get("benchmarks", {}))
    if not reusable:
        snapshot = collect_snapshot(sorted(tickers), sorted(benchmarks))
        publish_snapshot(snapshot, snapshot_root())
    reports = []
    for item in accounts:
        report = build_industry_report(item["account"], item["positions"], snapshot)
        write_report(report, ROOT / "outputs/industry_risk")
        reports.append({"account_id": report["account_id"], "status": report["status"]})
    return reports


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--accounts-export", type=Path)
    p.add_argument("--benchmark", help="Explicit report benchmark; does not change trading settings")
    p.add_argument("--snapshot-root", type=Path, default=snapshot_root())
    p.add_argument("--output", type=Path, default=ROOT / "outputs/industry_risk")
    p.add_argument("--env-file")
    args = p.parse_args()
    load_local_env(args.env_file)
    if args.accounts_export:
        accounts = json.loads(args.accounts_export.read_text())["accounts"]
    else:
        from src.papertrading.store import list_accounts, load_account, load_table
        accounts = [{"account": load_account(a["id"]), "positions": load_table(a["id"], "positions").to_dict("records")} for a in list_accounts()]
    tickers = [str(p["ticker"]) for a in accounts for p in a["positions"]]
    benchmarks = [args.benchmark or benchmark_for(a["account"]) for a in accounts]
    snapshot = collect_snapshot(tickers, [b for b in benchmarks if b])
    # Build all reports before publishing the observation pointer.
    reports = [build_industry_report(a["account"], pd.DataFrame(a["positions"]), snapshot, benchmark=b) for a, b in zip(accounts, benchmarks)]
    published = publish_snapshot(snapshot, args.snapshot_root)
    for report in reports:
        write_report(report, args.output)
    print(json.dumps({"snapshot": str(published), "reports": [{"account_id": r["account_id"], "status": r["status"], "issues": r["issues"]} for r in reports], "provider_errors": snapshot["errors"]}, ensure_ascii=False))
    return 0 if all(r["status"] == "READY" for r in reports) else 2


if __name__ == "__main__":
    raise SystemExit(main())
