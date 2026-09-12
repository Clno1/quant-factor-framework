"""Read saved current-version signals and collect isolated retrospective evidence."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.breakouts.live.cup_handle import CUP_HANDLE_ALGORITHM_VERSION, CUP_HANDLE_PARAMETER_VERSION
from src.breakouts.live.cup_handle_followthrough import assess_followthrough, summarize_followthrough
from src.breakouts.live.settings import IntradayMonitorSettings
from src.data.fmp import _get
from src.utils.env import load_local_env
from src.utils.io import atomic_save_json


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=date.fromisoformat, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--state-path", type=Path, default=ROOT / "outputs/intraday_momentum_monitor/state.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/data_audits/cup_followthrough")
    args = parser.parse_args()
    if load_local_env(args.env_file) is None:
        parser.error("missing env file")
    settings = IntradayMonitorSettings.load()
    with sqlite3.connect(args.state_path.resolve().as_uri() + "?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        signals = [dict(row) for row in con.execute(
            "SELECT * FROM signals WHERE session_date=? AND algorithm_version=? ORDER BY ticker",
            (str(args.session), CUP_HANDLE_ALGORITHM_VERSION))]
        snapshots = [dict(row) for row in con.execute(
            "SELECT * FROM candidate_snapshots WHERE session_date=?", (str(args.session),))]
    if len(signals) > 20:
        parser.error("more than 20 signals requires a separately bounded audit")
    output = args.output_dir / f"{args.session}_{uuid4().hex}"
    output.mkdir(parents=True)
    results = []
    for saved in signals:
        signal = json.loads(saved["payload_json"])
        binding = {"signal": signal, "signal_payload_sha256": sha(saved["payload_json"]),
                   "delivery_state": saved["delivery_state"], "first_seen_at": saved["first_seen_at"]}
        candidates = []
        for snapshot in snapshots:
            payload = json.loads(snapshot["payload_json"])
            cup = payload.get("cup_handle_daily", {})
            if (cup.get("algorithm_version") == CUP_HANDLE_ALGORITHM_VERSION
                    and cup.get("parameter_version") == CUP_HANDLE_PARAMETER_VERSION
                    and datetime.fromisoformat(snapshot["created_at"]) <= datetime.fromisoformat(signal["triggered_at"])
                    and any(r.get("ticker") == saved["ticker"] and r.get("cup_qualified") for r in payload.get("rows", []))):
                candidates.append(snapshot)
        if signal.get("parameter_version") != CUP_HANDLE_PARAMETER_VERSION or len(candidates) != 1:
            results.append({**binding, "status": "UNRESOLVED", "false_positive_proxy": None,
                            "reason": "MISSING_OR_AMBIGUOUS_CURRENT_CANDIDATE_CONTRACT"})
            continue
        snapshot = candidates[0]
        name = f"{saved['ticker']}_candidate.json"
        atomic_save_json(json.loads(snapshot["payload_json"]), output / name)
        binding.update(candidate_artifact=name, candidate_payload_sha256=sha(snapshot["payload_json"]),
                       candidate_artifact_sha256=hashlib.sha256((output / name).read_bytes()).hexdigest())
        try:
            raw = _get("/historical-chart/1min", params={"symbol": saved["ticker"],
                        "from": str(args.session), "to": str(args.session)})
            name = f"{saved['ticker']}_1min_response.json"
            atomic_save_json(raw, output / name)
            binding.update(response_artifact=name, response_sha256=hashlib.sha256((output / name).read_bytes()).hexdigest())
            rows = raw if isinstance(raw, list) else raw.get("historical", []) if isinstance(raw, dict) else []
            assessment = assess_followthrough(signal, rows,
                horizon_bars=settings.cup_replay_confirmation_horizon_bars,
                target_return_pct=settings.cup_replay_confirmation_return_pct)
        except Exception as exc:
            assessment = {"status": "UNRESOLVED", "false_positive_proxy": None,
                          "reason": "PROVIDER_REQUEST_FAILED", "error_type": type(exc).__name__}
        results.append({**binding, **assessment})
    report = {"audit_version": "cup-followthrough-v1", "observed_at": datetime.now(timezone.utc).isoformat(),
              "algorithm_version": CUP_HANDLE_ALGORITHM_VERSION, "parameter_version": CUP_HANDLE_PARAMETER_VERSION,
              "session": str(args.session), "historical_observation_unchanged": True,
              "counts_for_shadow_promotion": False, "execution_pnl": False,
              "interpretation": "Posthoc bar-based proxy from later provider responses, not live availability or execution proof.",
              "candidate_binding": "Saved pre-signal snapshot containing the current cup sub-contract; not a fresh certification of archived coverage/PIT artifacts.",
              "config_sha256": hashlib.sha256((ROOT / "configs/default.yaml").read_bytes()).hexdigest(),
              "settings": {"horizon_bars": settings.cup_replay_confirmation_horizon_bars,
                           "target_return_pct": settings.cup_replay_confirmation_return_pct},
              "summary": summarize_followthrough(results), "results": results}
    atomic_save_json(report, output / "audit.json")
    print(json.dumps({"report": str(output / "audit.json"), "summary": report["summary"],
                      "results": [{"ticker": r["signal"]["ticker"], "status": r["status"],
                                   "reason": r.get("reason")} for r in results]}))


if __name__ == "__main__":
    main()
