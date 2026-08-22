#!/usr/bin/env python3
"""Check whether US_LIQUID_5M may publish formal factor research."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import CONFIG  # noqa: E402
from src.data.foundation import MarketDataCatalog, MarketDataReader  # noqa: E402
from src.data.security_master_store import SecurityMasterStore  # noqa: E402
from src.data.universe_ids import US_LIQUID_5M  # noqa: E402
from src.data.universe_publication import DerivedUniverseStore  # noqa: E402
from src.factors.broad_research_gate import (  # noqa: E402
    assess_broad_research_readiness,
)
from src.factors.data_publication import FactorDataStore  # noqa: E402
from src.utils.io import atomic_save_json  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-path")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _default_report_path() -> Path:
    root = CONFIG.abs_path(str(CONFIG.data.broad_factor_research.output_dir))
    return root / "broad_research_readiness.json"


def run() -> dict[str, Any]:
    catalog = MarketDataCatalog(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path))
    )
    market_reader = MarketDataReader(catalog=catalog)
    universe_store = DerivedUniverseStore(
        catalog=catalog,
        snapshot_root=CONFIG.abs_path(
            str(CONFIG.data.broad_universe.snapshot_dir)
        ),
        market_reader=market_reader,
    )
    factor_store = FactorDataStore(
        market_reader=market_reader,
        universe_store=universe_store,
    )
    publication = factor_store.load_publication(verify_partitions=False)
    universe_version = universe_store.require_latest(US_LIQUID_5M)
    membership = universe_store.load_membership(
        US_LIQUID_5M,
        version_id=universe_version.universe_version_id,
    )
    security_settings = CONFIG.data.security_master
    security_store = SecurityMasterStore(
        CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
        CONFIG.abs_path(str(security_settings.snapshot_dir)),
    )
    security_generation, frames = security_store.load_published()
    if (
        publication.get("universe_version_id")
        != universe_version.universe_version_id
        or publication.get("security_master_generation_id")
        != security_generation.generation_id
        or publication.get("security_master_sha256")
        != security_generation.manifest_sha256
    ):
        raise RuntimeError(
            "factor data, PIT universe and Security Master versions differ"
        )
    settings = CONFIG.data.broad_factor_research
    result = assess_broad_research_readiness(
        publication=publication,
        membership=membership,
        classifications=frames["classifications"],
        preprocessing_audit=factor_store.preprocessing_audit(publication),
        expected_factor_ids=CONFIG.factors.enabled,
        minimum_evaluable_sessions=int(settings.minimum_evaluable_sessions),
        minimum_cross_section=int(settings.minimum_cross_section),
        minimum_pit_industry_coverage=float(
            settings.minimum_pit_industry_coverage
        ),
        accepted_classification_policies=(
            settings.accepted_classification_policies
        ),
    )
    return {
        **result.to_dict(),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "parent_dataset_version_id": publication.get(
            "parent_dataset_version_id"
        ),
        "universe_version_id": publication.get("universe_version_id"),
        "security_master_generation_id": publication.get(
            "security_master_generation_id"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report_path = (
        Path(args.report_path).expanduser().resolve()
        if args.report_path else _default_report_path()
    )
    infrastructure_failure = False
    try:
        report = run()
    except Exception as exc:  # noqa: BLE001
        infrastructure_failure = True
        report = {
            "schema_version": 1,
            "status": "BLOCKED",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "blockers": ["FOUNDATION_NOT_READY"],
            "checks": [],
            "error": str(exc),
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_save_json(report, report_path)
    report["report_path"] = str(report_path)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(
            f"broad research readiness: {report['status']} "
            f"blockers={report.get('blockers') or []}"
        )
        print(f"report: {report_path}")
    if infrastructure_failure:
        return 3
    return 0 if report["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
