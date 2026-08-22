#!/usr/bin/env python3
"""Propose an audited research-history policy from frozen production evidence.

This command never changes Security Master data or the approved registry. It
only joins a frozen candidate's interval diagnostics with a failed historical
coverage checkpoint and emits a reviewable YAML proposal.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from src.data.broad_coverage import select_coverage_securities
from src.data.research_history_policy import (
    EXCLUDED_UNVERIFIABLE_HISTORY,
    PROSPECTIVE_ONLY,
    load_research_history_policy,
)


SECURITY_ID_PATTERN = re.compile(r"sec_[0-9a-f]{32}")
INVALID_INTERVAL_PATTERN = re.compile(r"^invalid interval\s+(\S+)\s+")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Propose exact historical-research policy entries."
    )
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--coverage-checkpoint", required=True)
    parser.add_argument("--activation-session", required=True)
    parser.add_argument("--approved-at", default=date.today().isoformat())
    parser.add_argument("--approved-by", default="project_owner")
    parser.add_argument(
        "--existing-policy",
        help="approved registry whose exact entries must be retained",
    )
    return parser.parse_args()


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def build_proposal(
    *,
    candidate_dir: Path,
    coverage_checkpoint: Path,
    activation_session: str,
    approved_at: str,
    approved_by: str,
    existing_policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build a deterministic proposal and diagnostics from frozen artifacts."""
    master = pd.read_parquet(candidate_dir / "master.parquet")
    symbols = pd.read_parquet(candidate_dir / "symbols.parquet")
    audit = _load_json(candidate_dir / "audit.json")
    checkpoint = _load_json(coverage_checkpoint)
    existing_entries = {
        str(item["security_id"]): item
        for item in (existing_policy or {}).get("entries") or []
    }
    existing_policy_frame = pd.DataFrame([
        {
            "security_id": security_id,
            "policy": item["policy"],
            "effective_from": item["effective_from"],
        }
        for security_id, item in existing_entries.items()
    ])

    target = pd.Timestamp(activation_session).normalize()
    selected = select_coverage_securities(
        master,
        history_start=str(checkpoint["history_start"]),
        target_session=target,
        history_policy=existing_policy_frame,
    )
    selected_ids = set(selected["security_id"].astype(str))
    master_ids = set(master["security_id"].astype(str))

    interval_messages = list(
        (audit.get("quality") or {}).get("ticker_interval_conflicts") or []
    )
    interval_ids = {
        match
        for message in interval_messages
        for match in SECURITY_ID_PATTERN.findall(str(message))
    }
    for message in interval_messages:
        match = INVALID_INTERVAL_PATTERN.match(str(message))
        if match is None:
            continue
        ticker = match.group(1).strip().upper()
        interval_ids.update(
            symbols.loc[
                symbols["ticker"].astype(str).str.upper().eq(ticker),
                "security_id",
            ].astype(str)
        )
    failure_ids = {
        str(item.get("security_id") or "")
        for item in checkpoint.get("alias_failures") or []
        if isinstance(item, dict) and item.get("security_id")
    }
    provider_ids = failure_ids & selected_ids
    missing_existing_ids = sorted(set(existing_entries) - master_ids)
    if missing_existing_ids:
        raise ValueError(
            "existing policy identities are absent from the candidate: "
            + ", ".join(missing_existing_ids)
        )
    proposed_ids = sorted(
        set(existing_entries) | ((interval_ids | provider_ids) & master_ids)
    )

    rows = master.set_index("security_id", drop=False)
    entries: list[dict[str, Any]] = []
    for security_id in proposed_ids:
        row = rows.loc[security_id]
        if isinstance(row, pd.DataFrame):
            raise ValueError(f"duplicate Security Master row: {security_id}")
        status = str(row["trading_status"]).strip().upper()
        reasons = {
            str(value).strip().upper()
            for value in existing_entries.get(security_id, {}).get(
                "reason_codes", []
            )
            if str(value).strip()
        }
        if security_id in interval_ids:
            reasons.add("UNVERIFIABLE_TICKER_INTERVAL")
        if security_id in provider_ids:
            reasons.add("FMP_HISTORY_UNVERIFIABLE")
        effective_from = existing_entries.get(security_id, {}).get(
            "effective_from",
            target.date().isoformat(),
        )
        entries.append({
            "security_id": security_id,
            "current_ticker": str(row["current_ticker"]).strip().upper(),
            "name": str(row["name"]).strip(),
            "trading_status": status,
            "policy": (
                PROSPECTIVE_ONLY
                if status == "ACTIVE"
                else EXCLUDED_UNVERIFIABLE_HISTORY
            ),
            "effective_from": str(effective_from),
            "reason_codes": sorted(reasons),
        })

    payload = {
        "schema_version": 1,
        "universe": "US_SECURITY_MASTER",
        "activation_session": target.date().isoformat(),
        "decision": {
            "approved_at": str(approved_at),
            "approved_by": str(approved_by),
            "basis": (
                "FMP cannot prove complete pre-activation history for these "
                "securities; unresolved history must not enter broad research."
            ),
        },
        "entries": entries,
    }
    diagnostics = {
        "candidate_dir": str(candidate_dir),
        "coverage_checkpoint": str(coverage_checkpoint),
        "candidate_target_session": audit.get("target_session"),
        "checkpoint_target_session": checkpoint.get("target_session"),
        "selected_security_count": len(selected_ids),
        "interval_security_count": len(interval_ids),
        "checkpoint_failure_security_count": len(failure_ids),
        "provider_failure_in_current_scope_count": len(provider_ids),
        "retained_existing_policy_count": len(existing_entries),
        "new_policy_identity_count": len(set(proposed_ids) - set(existing_entries)),
        "proposal_count": len(entries),
        "prospective_only_count": sum(
            item["policy"] == PROSPECTIVE_ONLY for item in entries
        ),
        "excluded_unverifiable_history_count": sum(
            item["policy"] == EXCLUDED_UNVERIFIABLE_HISTORY for item in entries
        ),
        "checkpoint_failures_outside_current_scope": sorted(
            failure_ids - selected_ids
        ),
    }
    return payload, diagnostics


def main() -> int:
    args = parse_args()
    existing_policy = None
    if args.existing_policy:
        existing_policy, _path, _sha = load_research_history_policy(
            args.existing_policy
        )
    payload, diagnostics = build_proposal(
        candidate_dir=Path(args.candidate_dir).resolve(),
        coverage_checkpoint=Path(args.coverage_checkpoint).resolve(),
        activation_session=args.activation_session,
        approved_at=args.approved_at,
        approved_by=args.approved_by,
        existing_policy=existing_policy,
    )
    print(yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        width=100,
    ), end="")
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
