#!/usr/bin/env python3
"""Run version-bound industry A/B research into an isolated output directory."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from src.analysis.industry_comparison import comparison_factors, compare_ic
from src.analysis.forward_outcomes import build_forward_outcomes
from src.backtest.quintile import build_tradable_mask
from src.backtest.quintile_v2 import quintile_backtest_v2
from src.config import CONFIG
from src.data.access import load_published_bundle
from src.data.classification_history import build_pit_sector_matrix
from src.data.pit import build_membership_mask
from src.factors import get_factor
from src.factors.artifacts import load_factor_matrix_bundle
from src.utils.io import atomic_save_json


def _read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path, dtype={"security_id": str, "ticker": str})


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--universe", default="SP500")
    p.add_argument("--factor", default="MOM_6M")
    p.add_argument("--history", type=Path, default=ROOT / "data/pit_classifications/history.parquet")
    p.add_argument("--symbols", type=Path, default=ROOT / "data/pit_classifications/symbols.parquet")
    p.add_argument("--start", default="2020-01-01")
    p.add_argument("--end", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    audit = {"status": "RUNNING", "universe": args.universe, "factor": args.factor, "generated_at": datetime.now(timezone.utc).isoformat(), "production_publication_changed": False}
    missing = [str(path) for path in (args.history, args.symbols) if not path.is_file()]
    if missing:
        audit.update(status="BLOCKED_PIT_INPUTS_MISSING", missing_inputs=missing)
        atomic_save_json(audit, args.output / "audit.json")
        print(audit["status"])
        return 2
    try:
        audit["input_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in (args.history, args.symbols)}
        bundle = load_published_bundle(requested_universe=args.universe, data_universe=args.universe, start=args.start, end=args.end, require_open=True, factor_ids=[args.factor], require_factor_publication=True)
        raw, _, manifest = load_factor_matrix_bundle(args.factor, universe=args.universe)
        if manifest["generation_id"] != bundle.contract.factor_generations[args.factor]:
            raise ValueError("Factor generation changed during comparison")
        prices = bundle.prices
        if prices is None:
            raise ValueError("Typed execution/total-return prices required")
        returns = prices.total_return_close.pct_change(fill_method=None)
        raw = raw.reindex(index=returns.index, columns=returns.columns).loc[args.start:args.end].dropna(how="all")
        membership, membership_audit = build_membership_mask(raw.index, raw.columns, args.universe, required=True, membership_override=bundle.membership)
        if membership is None:
            raise ValueError("Comparison requires dated membership")
        raw = raw.where(membership)
        sectors = build_pit_sector_matrix(_read(args.history), _read(args.symbols), raw.index, raw.columns)
        variants, diagnostics = comparison_factors(raw, sectors)
        audit["comparison"] = diagnostics
        audit["data_contract"] = bundle.contract.to_dict()
        horizon = int(CONFIG.ic_analysis.forward_periods)
        forward = build_forward_outcomes(returns, total_return_close_df=prices.total_return_close, eligible_mask=raw.notna(), membership_events=bundle.membership_events, periods=horizon)
        ic, summary = compare_ic(variants, returns, periods=horizon, resolved_forward_returns=forward.returns)
        ic.to_parquet(args.output / "ic.parquet")
        summary.to_csv(args.output / "ic_summary.csv")
        tradable = build_tradable_mask(index=raw.index, columns=raw.columns, returns_df=returns, price_df=prices.execution_close, open_df=prices.execution_open, volume_df=bundle.wide.get("volume"), timing="next_open")
        direction = get_factor(args.factor).direction
        for name, factor in variants.items():
            result = quintile_backtest_v2(factor, returns, factor_direction=direction, execution_open_df=prices.execution_open, execution_close_df=prices.execution_close, total_return_open_df=prices.total_return_open, total_return_close_df=prices.total_return_close, volume_df=bundle.wide.get("volume"), tradable_mask=tradable & membership, membership_mask=membership, membership_events=bundle.membership_events, benchmark_returns=bundle.benchmark_returns)
            result.group_metrics.to_csv(args.output / f"{name}_metrics.csv")
            result.group_nav.to_parquet(args.output / f"{name}_nav.parquet")
            factor.to_parquet(args.output / f"{name}_factor.parquet")
        audit.update(status="COMPLETE", factor_direction=direction, forward_periods=horizon, rebalance_mode=str(CONFIG.backtest.rebalance_mode), interpretation="Historical research comparison; no live weights or formal publication changed")
    except Exception as exc:
        audit.update(status="BLOCKED", error_type=type(exc).__name__, reason=str(exc))
        atomic_save_json(audit, args.output / "audit.json")
        print(f"BLOCKED: {type(exc).__name__}: {exc}")
        return 2
    atomic_save_json(audit, args.output / "audit.json")
    print("COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
