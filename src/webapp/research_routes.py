"""Read-only research-universe pages and version-aware APIs."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pandas as pd
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.config import CONFIG, PROJECT_ROOT
from src.data.foundation import MarketDataCatalog, MarketDataReader
from src.data.membership_state import replay_membership_states
from src.data.security_master_store import SecurityMasterStore
from src.data.universe_publication import DerivedUniverseStore
from src.factors import get_factor_catalog, list_factor_ids
from src.factors.data_publication import FactorDataStore
from src.factors.observations import (
    FactorObservationError,
    FactorObservationReader,
)
from src.factors.publication import (
    RESEARCH_PUBLICATION_SCHEMA_VERSION,
    research_publication_path,
)
from src.research_universes.publication import (
    CrossUniversePublicationError,
    load_cross_universe_publication,
)
from src.research_universes.models import FactorPublicationMode
from src.research_universes.registry import research_universe_registry
from src.utils.identifiers import InvalidResourceId, safe_path_component
from src.utils.io import load_json
from src.utils.market_calendar import latest_publishable_xnys_session
from src.webapp.market_regime_status import (
    MarketRegimeViewError,
    market_regime_chart_payload,
    market_regime_status_payload,
)
from src.webapp.research_labels import research_label


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))
router = APIRouter()
factor_observation_reader = FactorObservationReader()
_FACTOR_OBSERVATION_STATUS_CODES = (
    "VALID",
    "NOT_PIT_MEMBER",
    "CALCULATION_WINDOW_INSUFFICIENT",
    "RAW_MISSING",
    "CLEAN_MISSING",
    "CLASSIFICATION_MISSING",
    "DATA_QUALITY_REJECTED",
)


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _expected_session() -> str:
    delay = int(getattr(CONFIG.data.foundation, "close_delay_minutes", 120))
    return latest_publishable_xnys_session(
        delay_minutes=delay
    ).date().isoformat()


def _session_delay(expected: str, observed: str | None) -> int | None:
    if not observed:
        return None
    expected_ts = pd.Timestamp(expected).normalize()
    observed_ts = pd.Timestamp(observed).normalize()
    if observed_ts >= expected_ts:
        return 0
    try:
        import exchange_calendars as xcals

        calendar = xcals.get_calendar(
            "XNYS",
            start=(observed_ts - pd.Timedelta(days=3)).date().isoformat(),
            end=(expected_ts + pd.Timedelta(days=3)).date().isoformat(),
        )
        return int(
            len(
                calendar.sessions_in_range(
                    observed_ts.date().isoformat(),
                    expected_ts.date().isoformat(),
                )
            )
            - 1
        )
    except Exception:  # noqa: BLE001
        return int(len(pd.bdate_range(observed_ts, expected_ts)) - 1)


def _research_pointer_status(universe: str, expected: str) -> dict[str, Any]:
    path = research_publication_path(universe)
    if not path.exists():
        return {"status": "MISSING", "target_session": None}
    try:
        payload = load_json(path)
        data = payload.get("data_foundation")
        target = data.get("target_session") if isinstance(data, dict) else None
        if (
            payload.get("schema_version")
            != RESEARCH_PUBLICATION_SCHEMA_VERSION
            or payload.get("status") != "PUBLISHED"
        ):
            status = "INVALID"
        elif target != expected:
            status = "STALE"
        else:
            status = "PUBLISHED"
        return {
            "status": status,
            "target_session": target,
            "publication_id": payload.get("publication_id"),
            "path": str(path),
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "INVALID", "target_session": None, "reason": str(exc)}


def _cross_payload() -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None]:
    try:
        pointer, frame, _manifest = load_cross_universe_publication()
    except CrossUniversePublicationError as exc:
        return None, [], str(exc)
    rows: list[dict[str, Any]] = []
    for record in frame.to_dict(orient="records"):
        try:
            universes = json.loads(record.get("universes_json") or "{}")
        except json.JSONDecodeError:
            universes = {}
        rows.append(
            {
                "factor_id": record.get("factor_id"),
                "target_session": record.get("target_session"),
                "verdict": record.get("verdict"),
                "direction_consistent": record.get("direction_consistent"),
                "summary": record.get("summary"),
                "universes": universes,
            }
        )
    return pointer, rows, None


def _universe_frame_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    current = (
        frame["is_current_member"].fillna(False).astype(bool)
        if "is_current_member" in frame.columns
        else pd.Series(True, index=frame.index)
    )
    sector = frame.get("sector", pd.Series(None, index=frame.index))
    sector_text = sector.fillna("").astype(str).str.strip()
    known = sector_text.ne("") & sector_text.ne("UNKNOWN")
    return {
        "total": int(len(frame)),
        "current_members": int(current.sum()),
        "known": int(known.sum()),
        "coverage": float(known.mean()),
        "source": "universe.parquet",
    }


def _pit_metadata(universe_id: str) -> dict[str, Any] | None:
    root = _project_path(CONFIG.universe.point_in_time.membership_dir)
    path = root / f"{universe_id}.metadata.json"
    if not path.exists():
        return None
    try:
        payload = load_json(path)
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001
        return None


def research_universe_payload() -> list[dict[str, Any]]:
    expected = _expected_session()
    catalog = MarketDataCatalog()
    reader = MarketDataReader(catalog=catalog)
    derived_store = DerivedUniverseStore(
        catalog=catalog,
        snapshot_root=CONFIG.abs_path(str(CONFIG.data.broad_universe.snapshot_dir)),
        market_reader=reader,
    )
    factor_data_store = FactorDataStore(
        market_reader=reader,
        universe_store=derived_store,
    )
    output: list[dict[str, Any]] = []
    for entry in research_universe_registry().list():
        if entry.factor_publication_mode == FactorPublicationMode.FACTOR_DATA:
            version = derived_store.latest(entry.universe_id)
            manifest: dict[str, Any] = {}
            integrity_error: str | None = None
            parent = None
            if version is not None:
                try:
                    manifest = derived_store.verify(
                        version,
                        verify_parent_partition_children=False,
                    )
                    parent = reader.require_version(
                        str(entry.parent_data_universe),
                        version.parent_dataset_version_id,
                        verify_partition_children=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    integrity_error = str(exc)
            data_status = "MISSING"
            if version is not None:
                if integrity_error:
                    data_status = "INVALID"
                elif version.target_session.isoformat() != expected:
                    data_status = "STALE"
                else:
                    data_status = "PUBLISHED"
            factor_status = "MISSING"
            factor_pointer: dict[str, Any] | None = None
            factor_error: str | None = None
            try:
                factor_pointer = factor_data_store.load_publication(
                    verify_partitions=False
                )
                if factor_pointer.get("universe_version_id") != (
                    version.universe_version_id if version else None
                ):
                    factor_status = "INVALID"
                    factor_error = "因子数据与当前宽基股票池版本不一致"
                elif factor_pointer.get("target_session") != expected:
                    factor_status = "STALE"
                else:
                    factor_status = "PUBLISHED"
            except Exception as exc:  # noqa: BLE001
                factor_error = str(exc)
            accepted_policies = {
                str(value).upper()
                for value in CONFIG.data.broad_factor_research.accepted_classification_policies
            }
            classification_policy = str(
                (factor_pointer or {}).get("classification_policy") or ""
            ).upper()
            if factor_pointer and classification_policy not in accepted_policies:
                formal_research = {
                    "status": "BLOCKED",
                    "target_session": None,
                    "publication_id": None,
                    "reason_code": "PIT_CLASSIFICATION_POLICY",
                    "reason": (
                        "当前行业分类是最新快照回填，不是历史时点可知数据；"
                        "因此禁止发布宽基 IC、ICIR 和置信评估。"
                    ),
                }
            else:
                formal_research = {
                    "status": "MISSING",
                    "target_session": None,
                    "publication_id": None,
                    "reason_code": "BROAD_RESEARCH_NOT_PUBLISHED",
                    "reason": "尚未发布正式宽基研究。",
                }
            output.append({
                **entry.to_dict(),
                "data_status": data_status,
                "factor_data_status": factor_status,
                "research_status": formal_research["status"],
                "data_version_id": parent.version_id if parent else None,
                "universe_version_id": (
                    version.universe_version_id if version else None
                ),
                "target_session": (
                    version.target_session.isoformat() if version else None
                ),
                "current_members": (
                    version.current_member_count if version else None
                ),
                "historical_union": (
                    version.historical_member_count if version else None
                ),
                "industry_coverage": None,
                "classification": {},
                "quality_checks": manifest.get("quality_checks") or [],
                "integrity_error": integrity_error,
                "pit_metadata": None,
                "hashes": {
                    "bars_sha256": parent.checksum_sha256 if parent else None,
                    "universe_sha256": (
                        version.eligibility_sha256 if version else None
                    ),
                    "membership_sha256": (
                        version.membership_sha256 if version else None
                    ),
                    "manifest_sha256": (
                        version.manifest_sha256 if version else None
                    ),
                },
                "research": {
                    "status": factor_status,
                    "target_session": (
                        factor_pointer.get("target_session")
                        if factor_pointer else None
                    ),
                    "publication_id": (
                        factor_pointer.get("publication_id")
                        if factor_pointer else None
                    ),
                    "publication_mode": "FACTOR_DATA",
                    "reason": factor_error,
                },
                "formal_research": formal_research,
                "classification_policy": classification_policy or None,
            })
            continue

        version = catalog.latest_version(entry.universe_id)
        manifest: dict[str, Any] = {}
        universe_file: dict[str, Any] = {}
        integrity_error: str | None = None
        if version is not None:
            try:
                manifest = reader.verify_version(version)
                universe_file = _universe_frame_summary(
                    reader.load_universe(
                        entry.universe_id,
                        current_only=False,
                        version=version,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                integrity_error = str(exc)
        classification = (
            manifest.get("classification")
            if isinstance(manifest.get("classification"), dict)
            else {}
        )
        if not classification:
            classification = universe_file
        requires_membership = (
            entry.factor_publication_mode == FactorPublicationMode.FULL_RESEARCH
            and entry.membership_type.value != "STATIC"
        )
        complete_hashes = bool(
            version
            and version.checksum_sha256
            and version.universe_checksum_sha256
            and (version.membership_checksum_sha256 or not requires_membership)
            and version.manifest_checksum_sha256
        )
        data_status = "MISSING"
        if version:
            if not complete_hashes or integrity_error:
                data_status = "INVALID"
            elif version.target_session.isoformat() != expected:
                data_status = "STALE"
            else:
                data_status = "PUBLISHED"
        if entry.factor_publication_mode == FactorPublicationMode.FULL_RESEARCH:
            research = _research_pointer_status(entry.universe_id, expected)
            factor_data_status = research["status"]
            research_status = research["status"]
        else:
            research = {
                "status": "NOT_APPLICABLE",
                "target_session": None,
                "publication_mode": "RAW_ONLY",
            }
            factor_data_status = "NOT_APPLICABLE"
            research_status = "NOT_APPLICABLE"
        output.append(
            {
                **entry.to_dict(),
                "data_status": data_status,
                "factor_data_status": factor_data_status,
                "research_status": research_status,
                "data_version_id": version.version_id if version else None,
                "universe_version_id": None,
                "target_session": version.target_session.isoformat() if version else None,
                "current_members": (
                    int(manifest.get("current_ticker_count"))
                    if manifest.get("current_ticker_count") is not None
                    else universe_file.get("current_members")
                ),
                "historical_union": int(version.ticker_count) if version else None,
                "industry_coverage": classification.get("coverage"),
                "classification": classification,
                "quality_checks": manifest.get("quality_checks") or [],
                "integrity_error": integrity_error,
                "pit_metadata": _pit_metadata(entry.universe_id),
                "hashes": {
                    "bars_sha256": version.checksum_sha256 if version else None,
                    "universe_sha256": (
                        version.universe_checksum_sha256 if version else None
                    ),
                    "membership_sha256": (
                        version.membership_checksum_sha256 if version else None
                    ),
                    "manifest_sha256": (
                        version.manifest_checksum_sha256 if version else None
                    ),
                },
                "research": research,
            }
        )
    return output


def research_status_payload() -> dict[str, Any]:
    try:
        expected = _expected_session()
        universes = research_universe_payload()
        formal = [
            item
            for item in universes
            if item["role"] in {"PRIMARY", "SECONDARY"}
        ]
        observed = [item["target_session"] for item in formal if item["target_session"]]
        market_session = min(observed) if observed else None
        pointer, _rows, cross_error = _cross_payload()
        cross_status = "MISSING"
        if pointer:
            if pointer.get("target_session") != expected:
                cross_status = "STALE"
            elif pointer.get("has_insufficient"):
                cross_status = "INSUFFICIENT"
            else:
                cross_status = "PUBLISHED"
        return {
            "expected_session": expected,
            "market_session": market_session,
            "market_status": (
                "PUBLISHED"
                if formal and all(item["data_status"] == "PUBLISHED" for item in formal)
                else "DEGRADED"
            ),
            "data_delay_sessions": _session_delay(expected, market_session),
            "universes": {
                item["universe_id"]: {
                    **item["research"],
                    "factor_data_status": item.get("factor_data_status"),
                    "formal_research_status": item.get("research_status"),
                    "formal_research": item.get("formal_research"),
                }
                for item in universes
            },
            "cross_status": cross_status,
            "cross_target_session": pointer.get("target_session") if pointer else None,
            "cross_error": cross_error,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "expected_session": None,
            "market_session": None,
            "market_status": "INVALID",
            "data_delay_sessions": None,
            "universes": {},
            "cross_status": "INVALID",
            "cross_target_session": None,
            "cross_error": str(exc),
        }


def _fmt(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"{number:.{digits}f}"


def _factor_page_rows(
    *,
    verdict: str | None = None,
    category: str | None = None,
    pool_id: str | None = None,
    pool_verdict: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    pointer, assessments, error = _cross_payload()
    cross_map = {str(item["factor_id"]): item for item in assessments}
    catalog = get_factor_catalog()
    rows: list[dict[str, Any]] = []
    for factor_id in list_factor_ids():
        meta = catalog[factor_id]
        assessment = cross_map.get(factor_id) or {
            "factor_id": factor_id,
            "target_session": pointer.get("target_session") if pointer else None,
            "verdict": "INSUFFICIENT",
            "direction_consistent": None,
            "summary": "尚无完整跨池研究发布",
            "universes": {},
        }
        if verdict and str(assessment["verdict"]).upper() != verdict.upper():
            continue
        if category and str(meta.category).lower() != category.lower():
            continue
        universe_evidence = assessment.get("universes") or {}
        primary = universe_evidence.get("SP500") or {}
        secondary = universe_evidence.get("NASDAQ100") or {}
        selected_pool = str(pool_id or "").upper()
        if selected_pool not in {"SP500", "NASDAQ100"}:
            selected_pool = ""
        if selected_pool and pool_verdict:
            selected_evidence = universe_evidence.get(selected_pool) or {}
            observed_verdict = str(
                selected_evidence.get("verdict")
                or selected_evidence.get("status")
                or "MISSING"
            ).upper()
            if observed_verdict != str(pool_verdict).upper():
                continue
        cross_verdict = str(assessment.get("verdict") or "INSUFFICIENT")
        market_scope = {
            "ROBUST": "SP500 与 NASDAQ100",
            "PRIMARY_ONLY": "SP500",
            "SEGMENT_SPECIFIC": "NASDAQ100",
            "CONFLICT": "暂不适合跨池泛化",
            "REJECT": "未发现可支持的研究池",
            "INSUFFICIENT": "证据不足",
        }.get(cross_verdict, "证据不足")
        market_limit = {
            "ROBUST": "仍需在具体目标股票池验证容量与成本",
            "PRIMARY_ONLY": "NASDAQ100 证据偏弱，不应直接外推",
            "SEGMENT_SPECIFIC": "SP500 尚未通过，不应解释为广泛有效",
            "CONFLICT": "两个核心池方向或显著性冲突",
            "REJECT": "两个核心池均未提供支持",
            "INSUFFICIENT": "至少一个核心研究发布缺失、陈旧或不可比",
        }.get(cross_verdict, "研究证据不足")
        rows.append(
            {
                **assessment,
                "display_name": meta.display_name,
                "category": meta.category,
                "formula": meta.formula,
                "description": meta.description,
                "direction": meta.direction,
                "risk_note": meta.risk_note,
                "sp500": primary,
                "nasdaq100": secondary,
                "ic_mean_display": _fmt(primary.get("ic_mean"), 4),
                "ic_ir_display": _fmt(primary.get("ic_ir"), 3),
                "q_value_display": _fmt(primary.get("q_value"), 4),
                "ls_sharpe_display": _fmt(primary.get("long_short_sharpe"), 3),
                "market_scope": market_scope,
                "market_limit": market_limit,
            }
        )
    return rows, pointer, error


def factor_assessment_map() -> dict[str, dict[str, Any]]:
    rows, _pointer, _error = _factor_page_rows()
    return {str(row["factor_id"]): row for row in rows}


def factor_research_snapshot(factor_ids: list[str]) -> dict[str, Any]:
    """Freeze current cross-pool evidence for a strategy or portfolio run."""
    rows, pointer, error = _factor_page_rows()
    selected = {str(value).upper() for value in factor_ids}
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "cross_publication": dict(pointer) if pointer else None,
        "publication_error": error,
        "factors": [row for row in rows if row["factor_id"] in selected],
    }


def _membership_detail(universe_id: str, version_id: str | None) -> dict[str, Any]:
    if not version_id:
        return {"current_members": [], "changes": [], "error": "DATA_VERSION_MISSING"}
    reader = MarketDataReader()
    try:
        version = reader.require_version(universe_id, version_id)
        metadata = reader.load_universe(
            universe_id,
            current_only=True,
            version=version,
        )
        membership = reader.load_membership(universe_id, version=version)
    except Exception as exc:  # noqa: BLE001
        return {"current_members": [], "changes": [], "error": str(exc)}

    member_columns = [
        column
        for column in ("ticker", "name", "sector", "sub_industry")
        if column in metadata.columns
    ]
    current_members = metadata[member_columns].fillna("").to_dict(orient="records")
    current_members.sort(key=lambda item: str(item.get("ticker") or ""))
    if membership is None or membership.empty:
        return {
            "current_members": current_members,
            "changes": [],
            "snapshot_count": 1,
            "start": version.min_date.isoformat() if version.min_date else None,
            "end": version.target_session.isoformat(),
            "error": None,
        }

    snapshots: list[tuple[pd.Timestamp, set[str]]] = []
    for snapshot_date, group in membership.groupby("date", sort=True):
        active = set(group.loc[group["active"], "ticker"].astype(str))
        snapshots.append((pd.Timestamp(snapshot_date), active))
    changes: list[dict[str, Any]] = []
    previous: set[str] | None = None
    for snapshot_date, active in snapshots:
        if previous is not None:
            additions = sorted(active - previous)
            removals = sorted(previous - active)
            if additions or removals:
                changes.append(
                    {
                        "date": snapshot_date.date().isoformat(),
                        "additions": additions,
                        "removals": removals,
                    }
                )
        previous = active
    return {
        "current_members": current_members,
        "changes": list(reversed(changes[-30:])),
        "snapshot_count": len(snapshots),
        "start": snapshots[0][0].date().isoformat(),
        "end": snapshots[-1][0].date().isoformat(),
        "error": None,
    }


def _derived_membership_detail(
    universe_id: str,
    universe_version_id: str | None,
) -> dict[str, Any]:
    if not universe_version_id:
        return {"current_members": [], "changes": [], "error": "UNIVERSE_VERSION_MISSING"}
    catalog = MarketDataCatalog()
    reader = MarketDataReader(catalog=catalog)
    store = DerivedUniverseStore(
        catalog=catalog,
        snapshot_root=CONFIG.abs_path(str(CONFIG.data.broad_universe.snapshot_dir)),
        market_reader=reader,
    )
    try:
        version = store.get(universe_id, universe_version_id)
        if version is None:
            raise FileNotFoundError("derived universe version does not exist")
        membership = store.load_membership(
            universe_id, version_id=universe_version_id
        )
        security_settings = CONFIG.data.security_master
        security_store = SecurityMasterStore(
            CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
            CONFIG.abs_path(str(security_settings.snapshot_dir)),
        )
        security_generation, security_frames = security_store.load_published()
        if (
            security_generation.generation_id
            != version.security_master_generation_id
            or security_generation.manifest_sha256
            != version.security_master_manifest_sha256
        ):
            raise RuntimeError(
                "Security Master differs from the derived universe version"
            )
    except Exception as exc:  # noqa: BLE001
        return {"current_members": [], "changes": [], "error": str(exc)}

    event_dates = pd.DatetimeIndex(
        sorted(pd.to_datetime(membership["date"]).dt.normalize().unique())
    )
    snapshots: list[tuple[pd.Timestamp, pd.DataFrame]] = []
    for state in replay_membership_states(
        membership,
        event_dates,
        key_column="security_id",
        value_column="ticker",
    ):
        active = pd.DataFrame({
            "security_id": list(state.active_keys),
            "ticker": [
                state.value_by_key[security_id]
                for security_id in state.active_keys
            ],
        })
        snapshots.append((state.date, active))
    if not snapshots:
        return {"current_members": [], "changes": [], "error": "EMPTY_MEMBERSHIP"}

    master = security_frames["master"].copy()
    master["security_id"] = master["security_id"].astype(str)
    classifications = security_frames.get("classifications", pd.DataFrame()).copy()
    latest_classification = pd.DataFrame(columns=["security_id", "sector", "sub_industry"])
    if not classifications.empty:
        sort_columns = [
            column
            for column in ("knowledge_date", "effective_from", "source_asof")
            if column in classifications.columns
        ]
        if sort_columns:
            classifications = classifications.sort_values(sort_columns)
        latest_classification = classifications.drop_duplicates(
            "security_id", keep="last"
        )[["security_id", "sector", "sub_industry"]]
        latest_classification["security_id"] = latest_classification[
            "security_id"
        ].astype(str)
    current = snapshots[-1][1].merge(
        master[["security_id", "name", "current_ticker"]],
        on="security_id",
        how="left",
        validate="many_to_one",
    ).merge(
        latest_classification,
        on="security_id",
        how="left",
        validate="many_to_one",
    )
    current["ticker"] = current["ticker"].fillna(current["current_ticker"])
    current_members = current[
        ["ticker", "name", "sector", "sub_industry"]
    ].fillna("").sort_values("ticker").to_dict("records")

    changes: list[dict[str, Any]] = []
    previous: set[str] | None = None
    ticker_by_id = dict(zip(master["security_id"], master["current_ticker"], strict=True))
    for snapshot_date, group in snapshots:
        active = set(group["security_id"].astype(str))
        if previous is not None:
            additions = sorted(ticker_by_id.get(value, value) for value in active - previous)
            removals = sorted(ticker_by_id.get(value, value) for value in previous - active)
            if additions or removals:
                changes.append({
                    "date": snapshot_date.date().isoformat(),
                    "additions": additions,
                    "removals": removals,
                })
        previous = active
    return {
        "current_members": current_members,
        "changes": list(reversed(changes[-30:])),
        "snapshot_count": len(snapshots),
        "start": snapshots[0][0].date().isoformat(),
        "end": snapshots[-1][0].date().isoformat(),
        "error": None,
    }


def _membership_detail_for_item(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("factor_publication_mode") == "FACTOR_DATA":
        return _derived_membership_detail(
            str(item["universe_id"]), item.get("universe_version_id")
        )
    if item.get("factor_publication_mode") == "RAW_ONLY":
        return {
            "current_members": [],
            "changes": [],
            "snapshot_count": 0,
            "start": None,
            "end": item.get("target_session"),
            "error": None,
        }
    return _membership_detail(
        str(item["universe_id"]), item.get("data_version_id")
    )


def _universe_factor_rows(universe_id: str) -> list[dict[str, Any]]:
    rows, _pointer, _error = _factor_page_rows()
    output: list[dict[str, Any]] = []
    for row in rows:
        evidence = (row.get("universes") or {}).get(universe_id) or {}
        output.append(
            {
                "factor_id": row["factor_id"],
                "display_name": row["display_name"],
                "verdict": evidence.get("verdict") or evidence.get("status") or "MISSING",
                "ic_mean": evidence.get("ic_mean"),
                "ic_ir": evidence.get("ic_ir"),
                "q_value": evidence.get("q_value"),
                "research_publication_id": evidence.get("research_publication_id"),
            }
        )
    return output


@router.get("/research", response_class=HTMLResponse)
def research_overview(
    request: Request,
    view: str = Query("factors"),
    verdict: str | None = Query(None),
    category: str | None = Query(None),
    pool: str | None = Query(None),
    pool_verdict: str | None = Query(None),
):
    selected_view = str(view or "factors").strip().lower()
    if selected_view not in {"factors", "universes"}:
        selected_view = "factors"
    rows, pointer, error = _factor_page_rows(
        verdict=verdict,
        category=category,
        pool_id=pool,
        pool_verdict=pool_verdict,
    )
    categories = sorted({entry.category for entry in get_factor_catalog().values()})
    return templates.TemplateResponse(
        request,
        "research_overview.html",
        {
            "title": "研究总览",
            "rows": rows,
            "publication": pointer,
            "publication_error": error,
            "selected_verdict": verdict or "",
            "selected_category": category or "",
            "selected_pool": (pool or "").upper(),
            "selected_pool_verdict": (pool_verdict or "").upper(),
            "categories": categories,
            "selected_view": selected_view,
            "universe_rows": (
                research_universe_payload()
                if selected_view == "universes"
                else []
            ),
        },
    )


@router.get("/research/market-regime", response_class=HTMLResponse)
def market_regime_page(request: Request):
    return templates.TemplateResponse(
        request,
        "market_regime_status.html",
        {
            "title": "市场研究状态",
            "market_regime": market_regime_status_payload(),
        },
    )


@router.get("/research/cross-universe", response_class=HTMLResponse)
def cross_universe_page(request: Request):
    del request
    return RedirectResponse(url="/research", status_code=307)


@router.get("/research/universes", response_class=HTMLResponse)
def research_universes_page(request: Request):
    del request
    return RedirectResponse(url="/research?view=universes", status_code=307)


@router.get("/research/universes/{universe_id}", response_class=HTMLResponse)
def research_universe_page(request: Request, universe_id: str):
    universe_id = universe_id.upper()
    item = next(
        (
            value
            for value in research_universe_payload()
            if value["universe_id"] == universe_id
        ),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Research universe not found")
    detail = _membership_detail_for_item(item)
    run_universe = item.get("parent_data_universe") or universe_id
    runs = MarketDataCatalog().list_ingestion_runs(run_universe, limit=12)
    return templates.TemplateResponse(
        request,
        "research_universe_detail.html",
        {
            "title": f"{universe_id} · 研究股票池",
            "universe": item,
            "membership": detail,
            "factor_rows": (
                _universe_factor_rows(universe_id)
                if item.get("factor_publication_mode") == "FULL_RESEARCH"
                else []
            ),
            "ingestion_runs": runs,
        },
    )


@router.get("/research/factors/{factor_id}", response_class=HTMLResponse)
def research_factor_page(request: Request, factor_id: str):
    try:
        factor_id = safe_path_component(factor_id.upper(), label="factor_id")
    except InvalidResourceId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    catalog = get_factor_catalog()
    if factor_id not in catalog:
        raise HTTPException(status_code=404, detail="Factor not found")
    rows, pointer, error = _factor_page_rows()
    assessment = next((row for row in rows if row["factor_id"] == factor_id), None)
    if assessment is None:
        raise HTTPException(status_code=404, detail="Factor assessment not found")
    return templates.TemplateResponse(
        request,
        "research_factor.html",
        {
            "title": f"{factor_id} · 因子研究",
            "factor": catalog[factor_id],
            "assessment": assessment,
            "publication": pointer,
            "publication_error": error,
            "preprocessing": {
                "winsorize_method": CONFIG.preprocessing.winsorize_method,
                "winsorize_n": CONFIG.preprocessing.winsorize_n,
                "standardize": CONFIG.preprocessing.standardize,
                "neutralize_industry": CONFIG.preprocessing.neutralize_industry,
                "neutralize_mcap": CONFIG.preprocessing.neutralize_mcap,
            },
        },
    )


def _raise_observation_error(exc: FactorObservationError) -> None:
    raise HTTPException(
        status_code=exc.status_code,
        detail=exc.to_dict(),
    ) from exc


@router.get("/research/factor-data", response_class=HTMLResponse)
def factor_data_page(
    request: Request,
    mode: str = Query("snapshot"),
    universe: str | None = Query(None),
    factor: str | None = Query(None),
    date: str = Query("latest"),
    ticker: str | None = Query(None),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    selected_mode = str(mode).strip().lower()
    if selected_mode not in {"snapshot", "history"}:
        selected_mode = "snapshot"
    initial_state = {
        "mode": selected_mode,
        "universe": str(universe or "").strip().upper(),
        "factor": str(factor or "").strip().upper(),
        "date": str(date or "latest").strip(),
        "ticker": str(ticker or "").strip().upper(),
        "start": str(start or "").strip(),
        "end": str(end or "").strip(),
        "status_labels": {
            code: research_label(code)
            for code in _FACTOR_OBSERVATION_STATUS_CODES
        },
    }
    return templates.TemplateResponse(
        request,
        "factor_data.html",
        {
            "title": "因子数据",
            "initial_state": initial_state,
        },
    )


@router.get("/api/research/factor-data/meta")
def api_factor_data_meta(
    universe: str | None = Query(None),
    factor: str | None = Query(None),
):
    try:
        return factor_observation_reader.metadata(
            selected_universe=(universe or "").strip() or None,
            selected_factor=(factor or "").strip() or None,
        )
    except FactorObservationError as exc:
        _raise_observation_error(exc)


@router.get("/api/securities/search")
def api_security_search(
    q: str = Query(..., min_length=1),
    asof: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
):
    try:
        return factor_observation_reader.search_securities(
            query=q,
            asof=(asof or "").strip() or None,
            limit=limit,
        )
    except FactorObservationError as exc:
        _raise_observation_error(exc)


@router.get("/api/research/factor-data/snapshot")
def api_factor_data_snapshot(
    universe: str = Query(...),
    factor: str = Query(...),
    date: str = Query("latest"),
    ticker: str | None = Query(None),
    status: str = Query("all"),
    sort: str = Query("rank"),
    order: str = Query("asc"),
    offset: int = Query(0),
    limit: int = Query(100),
):
    try:
        return factor_observation_reader.snapshot(
            universe=universe,
            factor_id=factor,
            observation_date=date,
            ticker=ticker,
            status=status,
            sort=sort,
            order=order,
            offset=offset,
            limit=limit,
        ).to_dict()
    except FactorObservationError as exc:
        _raise_observation_error(exc)


@router.get("/api/research/factor-data/history")
def api_factor_data_history(
    universe: str = Query(...),
    factor: str = Query(...),
    ticker: str = Query(...),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    try:
        return factor_observation_reader.history(
            universe=universe,
            factor_id=factor,
            ticker=ticker,
            start=start,
            end=end,
        ).to_dict()
    except FactorObservationError as exc:
        _raise_observation_error(exc)


def _factor_export_frame(
    rows: list[dict[str, Any]], contract: dict[str, Any]
) -> pd.DataFrame:
    identity_fields = (
        "publication_mode",
        "publication_id",
        "publication_target_session",
        "factor_generation_id",
        "factor_manifest_sha256",
        "dataset_version_id",
        "dataset_manifest_sha256",
        "parent_dataset_version_id",
        "universe_version_id",
        "membership_sha256",
        "eligibility_sha256",
        "security_master_generation_id",
        "security_master_sha256",
        "normalization_universe_id",
        "preprocessing_methodology_version",
        "classification_policy",
    )
    identity = {field: contract.get(field) for field in identity_fields}
    return pd.DataFrame([{**row, **identity} for row in rows])


@router.get("/api/research/factor-data/export")
def api_factor_data_export(
    universe: str = Query(...),
    factor: str = Query(...),
    mode: str = Query("snapshot"),
    date: str = Query("latest"),
    ticker: str | None = Query(None),
    status: str = Query("all"),
    sort: str = Query("rank"),
    order: str = Query("asc"),
    start: str | None = Query(None),
    end: str | None = Query(None),
):
    selected_mode = str(mode).strip().lower()
    try:
        if selected_mode == "snapshot":
            result = factor_observation_reader.snapshot(
                universe=universe,
                factor_id=factor,
                observation_date=date,
                ticker=ticker,
                status=status,
                sort=sort,
                order=order,
                offset=0,
                limit=5000,
            )
            payload = result.to_dict()
            frame = _factor_export_frame(
                payload["rows"], payload["contract"]
            )
            period = payload["summary"]["observation_date"]
        elif selected_mode == "history":
            if not str(ticker or "").strip():
                raise FactorObservationError(
                    "INVALID_QUERY",
                    "导出单股历史必须提供股票代码。",
                    status_code=400,
                )
            result = factor_observation_reader.history(
                universe=universe,
                factor_id=factor,
                ticker=str(ticker),
                start=start,
                end=end,
            )
            payload = result.to_dict()
            frame = _factor_export_frame(
                payload["rows"], payload["contract"]
            )
            period = f"{payload['actual_start']}_{payload['actual_end']}"
        else:
            raise FactorObservationError(
                "INVALID_QUERY",
                f"不支持的导出模式：{mode}",
                status_code=400,
            )
    except FactorObservationError as exc:
        _raise_observation_error(exc)

    publication_prefix = payload["contract"]["publication_id"][:8]
    filename = (
        f"factor_data_{payload['contract']['universe']}_"
        f"{payload['contract']['factor_id']}_{selected_mode}_"
        f"{period}_{publication_prefix}.csv"
    )
    csv_text = "\ufeff" + frame.to_csv(index=False)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename={filename}; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


@router.get("/api/research/status")
def api_research_status():
    return research_status_payload()


@router.get("/api/research/market-regime/status")
def api_market_regime_status():
    return market_regime_status_payload()


@router.get("/api/research/market-regime/chart")
def api_market_regime_chart(
    instrument: str = Query("spx"),
    period: str = Query("recent"),
):
    try:
        return market_regime_chart_payload(
            instrument=instrument,
            period=period,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MarketRegimeViewError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/research/universes")
def api_research_universes():
    return {"universes": research_universe_payload()}


@router.get("/api/research/universes/{universe_id}")
def api_research_universe(universe_id: str):
    universe_id = universe_id.upper()
    item = next(
        (
            value
            for value in research_universe_payload()
            if value["universe_id"] == universe_id
        ),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Research universe not found")
    return {
        **item,
        "membership": _membership_detail_for_item(item),
        "factors": (
            _universe_factor_rows(universe_id)
            if item.get("factor_publication_mode") == "FULL_RESEARCH"
            else []
        ),
        "ingestion_runs": MarketDataCatalog().list_ingestion_runs(
            item.get("parent_data_universe") or universe_id,
            limit=12,
        ),
    }


@router.get("/api/research/factors")
def api_research_factors(
    verdict: str | None = Query(None),
    category: str | None = Query(None),
    pool: str | None = Query(None),
    pool_verdict: str | None = Query(None),
):
    rows, pointer, error = _factor_page_rows(
        verdict=verdict,
        category=category,
        pool_id=pool,
        pool_verdict=pool_verdict,
    )
    return {"publication": pointer, "error": error, "factors": rows}


@router.get("/api/research/factors/{factor_id}/cross-universe")
def api_factor_cross_universe(factor_id: str):
    factor_id = factor_id.upper()
    rows, pointer, error = _factor_page_rows()
    item = next((row for row in rows if row["factor_id"] == factor_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Factor assessment not found")
    return {"publication": pointer, "error": error, "assessment": item}


@router.get("/api/research/factors/{factor_id}")
def api_research_factor(factor_id: str):
    factor_id = factor_id.upper()
    catalog = get_factor_catalog()
    if factor_id not in catalog:
        raise HTTPException(status_code=404, detail="Factor not found")
    rows, pointer, error = _factor_page_rows()
    item = next((row for row in rows if row["factor_id"] == factor_id), None)
    meta = catalog[factor_id]
    return {
        "factor": {
            "id": meta.id,
            "display_name": meta.display_name,
            "category": meta.category,
            "formula": meta.formula,
            "description": meta.description,
            "direction": meta.direction,
            "inputs": meta.inputs,
            "risk_note": meta.risk_note,
        },
        "publication": pointer,
        "error": error,
        "assessment": item,
    }


__all__ = [
    "factor_assessment_map",
    "factor_research_snapshot",
    "research_status_payload",
    "research_universe_payload",
    "router",
    "templates",
]
