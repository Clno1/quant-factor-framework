"""DuckDB-backed observations for broad long-form factor publications."""
from __future__ import annotations

from datetime import date
import math
from pathlib import Path
import time
from typing import Any, Callable

import duckdb
import pandas as pd

from src.config import CONFIG
from src.data.foundation import DataFoundationError
from src.data.membership_state import resolve_membership_asof
from src.data.security_master import UNKNOWN_CLASSIFICATION
from src.data.security_master_store import (
    SecurityMasterGeneration,
    SecurityMasterStore,
)
from src.factors.data_publication import FactorDataStore, FactorPartition
from src.factors.library import get_factor_catalog
from src.factors.observations import (
    MAX_HISTORY_SESSIONS,
    FactorHistoryResult,
    FactorObservationContract,
    FactorObservationError,
    FactorSnapshotResult,
)
from src.utils.identifiers import InvalidResourceId, canonical_ticker, safe_path_component
from src.utils.market_calendar import latest_publishable_xnys_session


_STATUS_FILTERS = {
    "all": None,
    "valid": "VALID",
    "not_pit_member": "NOT_PIT_MEMBER",
    "raw_missing": "RAW_MISSING",
    "clean_missing": "CLEAN_MISSING",
    "calculation_window_insufficient": "CALCULATION_WINDOW_INSUFFICIENT",
    "classification_missing": "CLASSIFICATION_MISSING",
    "data_quality_rejected": "DATA_QUALITY_REJECTED",
}

_SORT_FIELDS = {
    "rank": "factor_rank",
    "ticker": "ticker",
    "raw": "raw_value",
    "clean": "clean_value",
    "percentile": "factor_percentile",
}


def _date_text(value: Any, *, field: str) -> str:
    try:
        parsed = pd.Timestamp(value).normalize()
    except (TypeError, ValueError) as exc:
        raise FactorObservationError(
            "INVALID_QUERY",
            f"{field} 不是有效日期：{value!r}",
            status_code=400,
        ) from exc
    if pd.isna(parsed):
        raise FactorObservationError(
            "INVALID_QUERY", f"{field} 不是有效日期：{value!r}", status_code=400
        )
    return parsed.date().isoformat()


def _number(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _quintile(percentile: float | None) -> str | None:
    if percentile is None:
        return None
    return f"Q{min(5, max(1, int(math.ceil(percentile / 20.0))))}"


class BroadFactorObservationBackend:
    """Query one publication without loading its full history into Pandas."""

    def __init__(
        self,
        *,
        store: FactorDataStore | None = None,
        security_loader: Callable[
            [], tuple[SecurityMasterGeneration, dict[str, pd.DataFrame]]
        ] | None = None,
        expected_session: str | date | None = None,
        expected_session_provider: Callable[[], str] | None = None,
    ):
        self.store = store or FactorDataStore()
        if security_loader is None:
            settings = CONFIG.data.security_master
            security_store = SecurityMasterStore(
                CONFIG.abs_path(str(CONFIG.data.foundation.catalog_path)),
                CONFIG.abs_path(str(settings.snapshot_dir)),
            )
            security_loader = security_store.load_published
        self._security_loader = security_loader
        self._fixed_expected_session = (
            _date_text(expected_session, field="expected_session")
            if expected_session is not None
            else None
        )
        self._expected_session_provider = expected_session_provider
        self._metadata_cache: tuple[
            SecurityMasterGeneration, dict[str, pd.DataFrame]
        ] | None = None
        self._metadata_cached_at = 0.0

    def _expected_session(self) -> str:
        if self._fixed_expected_session is not None:
            return self._fixed_expected_session
        if self._expected_session_provider is not None:
            return _date_text(
                self._expected_session_provider(), field="expected_session"
            )
        return latest_publishable_xnys_session().date().isoformat()

    @staticmethod
    def _factor_id(value: str) -> str:
        try:
            factor_id = safe_path_component(value.upper(), label="factor_id")
        except InvalidResourceId as exc:
            raise FactorObservationError(
                "FACTOR_NOT_FOUND", f"因子不存在：{value}", status_code=404
            ) from exc
        if factor_id not in get_factor_catalog():
            raise FactorObservationError(
                "FACTOR_NOT_FOUND", f"因子不存在：{factor_id}", status_code=404
            )
        return factor_id

    def _publication(self, factor_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            publication = self.store.load_publication(verify_partitions=False)
        except DataFoundationError as exc:
            code = (
                "FACTOR_DATA_NOT_PUBLISHED"
                if "missing" in str(exc).lower()
                else "FACTOR_DATA_INVALID"
            )
            raise FactorObservationError(
                code,
                "全美宽基因子数据尚未发布。"
                if code == "FACTOR_DATA_NOT_PUBLISHED"
                else "全美宽基因子数据完整性校验失败，需要重新发布。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        if publication.get("universe") != "US_LIQUID_5M":
            raise FactorObservationError(
                "FACTOR_DATA_INVALID",
                "宽基因子 publication 的股票池身份错误。",
                status_code=409,
            )
        expected = self._expected_session()
        observed = str(publication.get("target_session") or "")
        if observed != expected:
            raise FactorObservationError(
                "FACTOR_DATA_STALE",
                f"全美宽基因子数据截止到 {observed or '未知日期'}，当前应发布到 {expected}。",
                status_code=409,
                details={"target_session": observed or None, "expected_session": expected},
            )
        binding = (publication.get("factors") or {}).get(factor_id)
        if not isinstance(binding, dict):
            raise FactorObservationError(
                "FACTOR_NOT_FOUND",
                f"当前宽基 publication 不包含因子 {factor_id}。",
                status_code=404,
            )
        try:
            manifest = self.store.factor_manifest(publication, factor_id)
        except DataFoundationError as exc:
            raise FactorObservationError(
                "FACTOR_DATA_INVALID",
                f"{factor_id} 因子 manifest 校验失败。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        return publication, manifest

    def _metadata(
        self, publication: dict[str, Any]
    ) -> dict[str, pd.DataFrame]:
        generation_id = str(publication["security_master_generation_id"])
        if (
            self._metadata_cache is not None
            and self._metadata_cache[0].generation_id == generation_id
            and self._metadata_cache[0].manifest_sha256
            == publication.get("security_master_sha256")
        ):
            return self._metadata_cache[1]
        try:
            generation, frames = self._security_loader()
        except Exception as exc:  # noqa: BLE001
            raise FactorObservationError(
                "FACTOR_DATA_INVALID",
                "Security Master 无法读取。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        if (
            generation.generation_id != generation_id
            or generation.manifest_sha256 != publication.get("security_master_sha256")
        ):
            raise FactorObservationError(
                "FACTOR_DATA_INVALID",
                "Security Master 与因子数据版本不一致。",
                status_code=409,
            )
        self._metadata_cache = (generation, frames)
        self._metadata_cached_at = time.monotonic()
        return frames

    def _search_metadata(
        self,
    ) -> tuple[SecurityMasterGeneration, dict[str, pd.DataFrame]]:
        if (
            self._metadata_cache is not None
            and time.monotonic() - self._metadata_cached_at <= 5.0
        ):
            return self._metadata_cache
        generation, frames = self._security_loader()
        self._metadata_cache = (generation, frames)
        self._metadata_cached_at = time.monotonic()
        return generation, frames

    def _contract(
        self,
        publication: dict[str, Any],
        manifest: dict[str, Any],
    ) -> FactorObservationContract:
        factor_id = str(manifest["factor_id"])
        entry = get_factor_catalog()[factor_id]
        direction = int(manifest.get("direction") or 0)
        if direction != int(entry.direction):
            raise FactorObservationError(
                "FACTOR_DATA_INVALID",
                f"{factor_id} 的预设方向与宽基 manifest 不一致。",
                status_code=409,
            )
        binding = publication["factors"][factor_id]
        return FactorObservationContract(
            schema_version=1,
            universe="US_LIQUID_5M",
            factor_id=factor_id,
            factor_name=entry.display_name,
            direction=direction,
            publication_id=str(publication["publication_id"]),
            publication_target_session=str(publication["target_session"]),
            factor_generation_id=str(binding["generation_id"]),
            factor_manifest_sha256=str(binding["manifest_sha256"]),
            dataset_version_id=str(publication["parent_dataset_version_id"]),
            dataset_manifest_sha256=str(
                publication["parent_dataset_manifest_sha256"]
            ),
            membership_sha256=str(publication["membership_sha256"]),
            membership_type="pit",
            date_start=str(binding["date_start"]),
            date_end=str(binding["date_end"]),
            classification_policy=str(publication["classification_policy"]),
            publication_mode="FACTOR_DATA",
            parent_dataset_version_id=str(publication["parent_dataset_version_id"]),
            universe_version_id=str(publication["universe_version_id"]),
            normalization_universe_id="US_LIQUID_5M",
            eligibility_sha256=str(publication["eligibility_sha256"]),
            security_master_generation_id=str(
                publication["security_master_generation_id"]
            ),
            security_master_sha256=str(publication["security_master_sha256"]),
            preprocessing_methodology_version=str(
                publication["preprocessing_methodology_version"]
            ),
        )

    def metadata(self, *, selected_factor: str | None = None) -> dict[str, Any]:
        """Return browser metadata without scanning the full observation lake."""
        factor_id = self._factor_id(selected_factor) if selected_factor else None
        try:
            publication = self.store.load_publication(verify_partitions=False)
        except DataFoundationError as exc:
            code = (
                "FACTOR_DATA_NOT_PUBLISHED"
                if "missing" in str(exc).lower()
                else "FACTOR_DATA_INVALID"
            )
            raise FactorObservationError(
                code,
                "全美宽基因子数据尚未发布。"
                if code == "FACTOR_DATA_NOT_PUBLISHED"
                else "全美宽基因子数据完整性校验失败，需要重新发布。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        expected = self._expected_session()
        observed = str(publication.get("target_session") or "")
        if observed != expected:
            raise FactorObservationError(
                "FACTOR_DATA_STALE",
                f"全美宽基因子数据截止到 {observed or '未知日期'}，当前应发布到 {expected}。",
                status_code=409,
                details={"target_session": observed or None, "expected_session": expected},
            )
        catalog = get_factor_catalog()
        factors: list[dict[str, Any]] = []
        for current_id, binding in (publication.get("factors") or {}).items():
            if current_id not in catalog:
                continue
            factors.append({
                "factor_id": current_id,
                "display_name": catalog[current_id].display_name,
                "direction": catalog[current_id].direction,
                "date_start": binding.get("date_start"),
                "date_end": binding.get("date_end"),
                "generation_id": binding.get("generation_id"),
            })
        factors.sort(key=lambda item: item["factor_id"])
        selected_binding = (
            (publication.get("factors") or {}).get(factor_id)
            if factor_id else None
        )
        accepted_policies = {
            str(value).upper()
            for value in CONFIG.data.broad_factor_research.accepted_classification_policies
        }
        classification_policy = str(
            publication.get("classification_policy") or ""
        ).upper()
        formal_research_status = (
            "MISSING"
            if classification_policy in accepted_policies
            else "BLOCKED"
        )
        return {
            "universe": {
                "status": "PUBLISHED",
                "factor_data_status": "PUBLISHED",
                "research_status": formal_research_status,
                "research_blockers": (
                    []
                    if formal_research_status == "MISSING"
                    else ["PIT_CLASSIFICATION_POLICY"]
                ),
                "web_default_enabled": bool(
                    getattr(
                        CONFIG.data.broad_factor_data,
                        "web_default_enabled",
                        False,
                    )
                ),
                "publication_id": publication["publication_id"],
                "publication_target_session": publication["target_session"],
                "dataset_version_id": publication["parent_dataset_version_id"],
                "parent_dataset_version_id": publication["parent_dataset_version_id"],
                "universe_version_id": publication["universe_version_id"],
                "capabilities": {
                    "raw": True,
                    "clean": True,
                    "rank": True,
                    "confidence": False,
                },
                "classification_policy": classification_policy,
                "factors": factors,
                "error": None,
            },
            # The date controls use the range; exact availability and adjacent
            # sessions are resolved against authenticated partitions at query time.
            "available_dates": (
                [str(selected_binding["date_end"])]
                if isinstance(selected_binding, dict) and selected_binding.get("date_end")
                else []
            ),
            "ticker_options": [],
        }

    def search_securities(
        self,
        *,
        query: str,
        asof: str | date | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search dated Security Master aliases and broad PIT membership."""
        text = str(query or "").strip().upper()
        if not text:
            raise FactorObservationError(
                "INVALID_QUERY", "请输入股票代码或公司名称。", status_code=400
            )
        if not 1 <= int(limit) <= 100:
            raise FactorObservationError(
                "INVALID_QUERY", "limit 必须在 1 到 100 之间。", status_code=400
            )
        try:
            generation, frames = self._search_metadata()
        except Exception as exc:  # noqa: BLE001
            raise FactorObservationError(
                "SECURITY_MASTER_NOT_PUBLISHED",
                "全美证券主表尚未发布或完整性校验失败。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        session = pd.Timestamp(asof or generation.target_session).normalize()
        if pd.isna(session):
            raise FactorObservationError(
                "INVALID_QUERY", f"asof 不是有效日期：{asof!r}", status_code=400
            )
        if session.date() > generation.target_session:
            raise FactorObservationError(
                "SECURITY_MASTER_STALE",
                f"证券主表截止到 {generation.target_session}，不能解析 {session.date()} 的股票身份。",
                status_code=409,
                details={
                    "security_master_target_session": generation.target_session.isoformat(),
                    "requested_asof": session.date().isoformat(),
                },
            )

        master = frames["master"].copy()
        master["security_id"] = master["security_id"].astype(str)
        for column, default in (
            ("current_ticker", ""),
            ("name", ""),
            ("primary_exchange", ""),
            ("asset_type", "UNKNOWN"),
            ("trading_status", "UNKNOWN"),
        ):
            if column not in master.columns:
                master[column] = default
            master[column] = master[column].fillna(default).astype(str)
        for column in ("listing_date", "delisting_date"):
            if column not in master.columns:
                master[column] = pd.NaT
            master[column] = pd.to_datetime(master[column], errors="coerce").dt.normalize()

        symbols = frames.get("symbols", pd.DataFrame()).copy()
        aliases = pd.DataFrame(columns=["security_id", "ticker"])
        if not symbols.empty:
            symbols["security_id"] = symbols["security_id"].astype(str)
            symbols["ticker"] = symbols["ticker"].fillna("").astype(str).str.upper()
            starts = pd.to_datetime(symbols["effective_from"], errors="coerce")
            ends = pd.to_datetime(symbols["effective_to"], errors="coerce")
            aliases = symbols.loc[
                (starts.isna() | starts.le(session))
                & (ends.isna() | ends.ge(session)),
                ["security_id", "ticker"],
            ].copy()
        candidates = master.merge(aliases, on="security_id", how="left")
        candidates["ticker"] = candidates["ticker"].fillna(
            candidates["current_ticker"]
        ).astype(str).str.upper()
        alive = (
            (candidates["listing_date"].isna() | candidates["listing_date"].le(session))
            & (candidates["delisting_date"].isna() | candidates["delisting_date"].ge(session))
        )
        candidates = candidates.loc[alive].copy()
        ticker_text = candidates["ticker"].str.upper()
        current_text = candidates["current_ticker"].str.upper()
        name_text = candidates["name"].str.upper()
        matched = (
            ticker_text.str.contains(text, regex=False)
            | current_text.str.contains(text, regex=False)
            | name_text.str.contains(text, regex=False)
            | candidates["security_id"].str.upper().eq(text)
        )
        candidates = candidates.loc[matched].copy()
        candidates["_score"] = 5
        candidates.loc[name_text.loc[candidates.index].str.startswith(text), "_score"] = 4
        candidates.loc[ticker_text.loc[candidates.index].str.contains(text, regex=False), "_score"] = 3
        candidates.loc[ticker_text.loc[candidates.index].str.startswith(text), "_score"] = 2
        candidates.loc[ticker_text.loc[candidates.index].eq(text), "_score"] = 1
        candidates.loc[
            candidates["security_id"].str.upper().eq(text), "_score"
        ] = 0
        candidates = (
            candidates.sort_values(["_score", "ticker", "name", "security_id"])
            .drop_duplicates("security_id", keep="first")
            .head(int(limit))
        )

        member_ids: set[str] = set()
        coverage_status = "MISSING"
        try:
            coverage = self.store.market_reader.require_latest(
                "US_EQUITY_COVERAGE",
                verify_partition_children=False,
            )
            coverage_manifest = self.store.market_reader.verify_version(
                coverage,
                verify_partition_children=False,
            )
            manifest_security_id = coverage_manifest.get(
                "security_master_generation_id"
            )
            manifest_security_sha = coverage_manifest.get(
                "security_master_manifest_sha256"
            )
            if manifest_security_id and (
                generation.generation_id != str(manifest_security_id)
                or generation.manifest_sha256 != str(manifest_security_sha)
            ):
                raise DataFoundationError(
                    "Security Master differs from coverage publication"
                )
            coverage_status = (
                "PUBLISHED"
                if session.date() <= coverage.target_session
                else "STALE"
            )
            universe_version = self.store.universe_store.require_latest(
                "US_LIQUID_5M",
                verify_parent_partition_children=False,
            )
            if (
                universe_version.parent_dataset_version_id != coverage.version_id
                or universe_version.security_master_generation_id
                != generation.generation_id
                or universe_version.security_master_manifest_sha256
                != generation.manifest_sha256
            ):
                raise DataFoundationError(
                    "broad universe differs from coverage or Security Master"
                )
            membership = self.store.universe_store.load_membership(
                "US_LIQUID_5M",
                version_id=universe_version.universe_version_id,
            )
            state = resolve_membership_asof(membership, session)
            member_ids = set(state["security_id"].astype(str))
        except (DataFoundationError, FileNotFoundError, KeyError, ValueError):
            member_ids = set()

        rows: list[dict[str, Any]] = []
        for row in candidates.to_dict("records"):
            security_id = str(row["security_id"])
            rows.append({
                "security_id": security_id,
                "ticker": str(row["ticker"]),
                "current_ticker": str(row["current_ticker"]),
                "name": str(row["name"] or row["ticker"]),
                "exchange": str(row["primary_exchange"]),
                "asset_type": str(row["asset_type"]),
                "listing_date": (
                    pd.Timestamp(row["listing_date"]).date().isoformat()
                    if pd.notna(row["listing_date"]) else None
                ),
                "delisting_date": (
                    pd.Timestamp(row["delisting_date"]).date().isoformat()
                    if pd.notna(row["delisting_date"]) else None
                ),
                "trading_status": "ACTIVE",
                "coverage_status": coverage_status,
                "available_comparison_universes": (
                    ["US_LIQUID_5M"] if security_id in member_ids else []
                ),
            })
        return {
            "security_master_generation_id": generation.generation_id,
            "security_master_target_session": generation.target_session.isoformat(),
            "asof": session.date().isoformat(),
            "query": text,
            "rows": rows,
        }

    def _entries(
        self,
        publication: dict[str, Any],
        factor_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[list[FactorPartition], list[Path]]:
        try:
            entries = self.store.partition_entries(
                publication, factor_id, start=start, end=end
            )
            paths = self.store.verified_partition_paths(entries)
        except DataFoundationError as exc:
            raise FactorObservationError(
                "FACTOR_DATA_INVALID",
                f"{factor_id} 因子分片校验失败。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        return entries, paths

    @staticmethod
    def _connect() -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect()
        connection.execute("SET threads = 1")
        return connection

    @staticmethod
    def _ranked_cte(direction: int) -> str:
        return f"""
            WITH base AS (
                SELECT date, security_id, ticker, raw_value, clean_value,
                       pit_member, status
                FROM read_parquet(?, hive_partitioning = false)
                WHERE date >= ? AND date <= ?
            ),
            scored AS (
                SELECT *,
                       CASE WHEN pit_member AND clean_value IS NOT NULL
                            THEN clean_value * {int(direction)} END AS oriented_value,
                       count(*) FILTER (
                           WHERE pit_member AND clean_value IS NOT NULL
                       ) OVER (PARTITION BY date) AS eligible_count
                FROM base
            ),
            ranked AS (
                SELECT *,
                       CASE WHEN oriented_value IS NOT NULL THEN
                           rank() OVER (
                               PARTITION BY date
                               ORDER BY oriented_value DESC NULLS LAST
                           )
                       END AS factor_rank,
                       CASE WHEN oriented_value IS NOT NULL THEN
                           100.0 * (
                               rank() OVER (
                                   PARTITION BY date
                                   ORDER BY oriented_value ASC NULLS LAST
                               )
                               + (
                                   count(*) OVER (
                                       PARTITION BY date, oriented_value
                                   ) - 1
                               ) / 2.0
                           ) / eligible_count
                       END AS factor_percentile
                FROM scored
            )
        """

    def _resolve_snapshot_date(
        self,
        publication: dict[str, Any],
        factor_id: str,
        manifest: dict[str, Any],
        requested: str,
    ) -> tuple[str, str | None, str | None]:
        selected = (
            str(manifest["date_end"])
            if str(requested).strip().lower() == "latest"
            else _date_text(requested, field="date")
        )
        all_entries = self.store.partition_entries(publication, factor_id)
        selected_ts = pd.Timestamp(selected)
        candidates = [
            item
            for item in all_entries
            if pd.Timestamp(item.date_end) >= selected_ts - pd.offsets.MonthBegin(1)
            and pd.Timestamp(item.date_start) <= selected_ts + pd.offsets.MonthEnd(1)
        ]
        if not candidates:
            previous = max(
                (item.date_end for item in all_entries if item.date_end < selected),
                default=None,
            )
            following = min(
                (item.date_start for item in all_entries if item.date_start > selected),
                default=None,
            )
            raise FactorObservationError(
                "DATE_NOT_AVAILABLE",
                f"{selected} 没有正式宽基因子观测。",
                status_code=422,
                details={"requested_date": selected, "previous_date": previous, "next_date": following},
            )
        try:
            paths = self.store.verified_partition_paths(candidates)
        except DataFoundationError as exc:
            raise FactorObservationError(
                "FACTOR_DATA_INVALID", "因子日期分片校验失败。", status_code=409,
                details={"reason": str(exc)},
            ) from exc
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT count(*) FILTER (WHERE date = ?) AS exact_rows,
                       max(date) FILTER (WHERE date < ?) AS previous_date,
                       min(date) FILTER (WHERE date > ?) AS next_date
                FROM read_parquet(?, hive_partitioning = false)
                """,
                [pd.Timestamp(selected).date()] * 3 + [[str(path) for path in paths]],
            ).fetchone()
        finally:
            connection.close()
        if not row or int(row[0] or 0) == 0:
            raise FactorObservationError(
                "DATE_NOT_AVAILABLE",
                f"{selected} 没有正式宽基因子观测。",
                status_code=422,
                details={
                    "requested_date": selected,
                    "previous_date": pd.Timestamp(row[1]).date().isoformat() if row and row[1] else None,
                    "next_date": pd.Timestamp(row[2]).date().isoformat() if row and row[2] else None,
                },
            )
        previous = pd.Timestamp(row[1]).date().isoformat() if row[1] else None
        following = pd.Timestamp(row[2]).date().isoformat() if row[2] else None
        return selected, previous, following

    @staticmethod
    def _metadata_maps(frames: dict[str, pd.DataFrame]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        master = frames["master"].copy()
        master["security_id"] = master["security_id"].astype(str)
        master_map = {
            str(row.security_id): row._asdict()
            for row in master.itertuples(index=False)
        }
        classifications = frames.get("classifications", pd.DataFrame()).copy()
        sector_map: dict[str, str] = {}
        if not classifications.empty:
            sort_columns = [
                column
                for column in ("knowledge_date", "effective_from", "source_asof")
                if column in classifications.columns
            ]
            if sort_columns:
                classifications = classifications.sort_values(sort_columns)
            latest = classifications.drop_duplicates("security_id", keep="last")
            sector_map = {
                str(row.security_id): str(row.sector or UNKNOWN_CLASSIFICATION)
                for row in latest.itertuples(index=False)
            }
        return master_map, sector_map

    @staticmethod
    def _decorate_rows(
        frame: pd.DataFrame,
        *,
        master_map: dict[str, dict[str, Any]],
        sector_map: dict[str, str],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in frame.to_dict("records"):
            security_id = str(row["security_id"])
            metadata = master_map.get(security_id, {})
            percentile = _number(row.get("factor_percentile"))
            rows.append({
                "date": pd.Timestamp(row["date"]).date().isoformat(),
                "security_id": security_id,
                "ticker": str(row["ticker"]),
                "name": str(metadata.get("name") or row["ticker"]),
                "sector": sector_map.get(security_id, UNKNOWN_CLASSIFICATION),
                "raw_value": _number(row.get("raw_value")),
                "clean_value": _number(row.get("clean_value")),
                "oriented_value": _number(row.get("oriented_value")),
                "factor_rank": int(row["factor_rank"]) if pd.notna(row.get("factor_rank")) else None,
                "eligible_count": int(row.get("eligible_count") or 0),
                "factor_percentile": percentile,
                "quintile": _quintile(percentile),
                "pit_member": bool(row["pit_member"]),
                "status": str(row["status"]),
            })
        return rows

    def _assert_current(self, contract: FactorObservationContract) -> None:
        try:
            current = self.store.load_publication(verify_partitions=False)
        except DataFoundationError as exc:
            raise FactorObservationError(
                "PUBLICATION_CHANGED",
                "查询期间宽基因子发布发生变化，请刷新后重试。",
                status_code=409,
            ) from exc
        binding = (current.get("factors") or {}).get(contract.factor_id) or {}
        observed = (
            current.get("publication_id"),
            current.get("parent_dataset_version_id"),
            current.get("universe_version_id"),
            binding.get("generation_id"),
            binding.get("manifest_sha256"),
        )
        expected = (
            contract.publication_id,
            contract.parent_dataset_version_id,
            contract.universe_version_id,
            contract.factor_generation_id,
            contract.factor_manifest_sha256,
        )
        if observed != expected:
            raise FactorObservationError(
                "PUBLICATION_CHANGED",
                "查询期间宽基因子发布发生变化，请刷新后重试。",
                status_code=409,
            )

    def snapshot(
        self,
        *,
        factor_id: str,
        observation_date: str = "latest",
        ticker: str | None = None,
        status: str = "all",
        sort: str = "rank",
        order: str = "asc",
        offset: int = 0,
        limit: int = 100,
    ) -> FactorSnapshotResult:
        factor_id = self._factor_id(factor_id)
        if int(offset) < 0 or not 1 <= int(limit) <= 5000:
            raise FactorObservationError(
                "INVALID_QUERY", "offset 必须不小于 0，limit 必须在 1 到 5000 之间。", status_code=400
            )
        normalized_status = str(status).strip().lower()
        if normalized_status not in _STATUS_FILTERS:
            raise FactorObservationError(
                "INVALID_QUERY", f"不支持的状态筛选：{status}", status_code=400
            )
        sort_key = str(sort).strip().lower()
        normalized_order = str(order).strip().lower()
        if sort_key not in _SORT_FIELDS or normalized_order not in {"asc", "desc"}:
            raise FactorObservationError(
                "INVALID_QUERY", "排序字段或方向无效。", status_code=400
            )
        publication, manifest = self._publication(factor_id)
        contract = self._contract(publication, manifest)
        selected, previous, following = self._resolve_snapshot_date(
            publication, factor_id, manifest, observation_date
        )
        _, paths = self._entries(
            publication, factor_id, start=selected, end=selected
        )
        path_values = [str(path) for path in paths]
        cte = self._ranked_cte(contract.direction)
        conditions: list[str] = []
        parameters: list[Any] = [
            path_values,
            pd.Timestamp(selected).date(),
            pd.Timestamp(selected).date(),
        ]
        status_value = _STATUS_FILTERS[normalized_status]
        if status_value is not None:
            conditions.append("status = ?")
            parameters.append(status_value)
        if ticker:
            conditions.append("upper(ticker) LIKE ?")
            parameters.append(f"%{str(ticker).strip().upper()}%")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sort_field = _SORT_FIELDS[sort_key]
        direction_sql = normalized_order.upper()
        ordering = (
            f"{sort_field} {direction_sql} NULLS LAST, ticker ASC"
            if sort_field != "ticker"
            else f"ticker {direction_sql}"
        )
        connection = self._connect()
        try:
            summary_row = connection.execute(
                """
                SELECT count(*) AS total_rows,
                       count(*) FILTER (WHERE pit_member) AS pit_members,
                       count(*) FILTER (WHERE pit_member AND raw_value IS NOT NULL) AS raw_valid,
                       count(*) FILTER (WHERE pit_member AND clean_value IS NOT NULL) AS clean_valid
                FROM read_parquet(?, hive_partitioning = false)
                WHERE date = ?
                """,
                [path_values, pd.Timestamp(selected).date()],
            ).fetchone()
            query = cte + f"""
                , filtered AS (
                    SELECT *, count(*) OVER () AS filtered_count
                    FROM ranked
                    {where}
                )
                SELECT * FROM filtered
                ORDER BY {ordering}
                LIMIT ? OFFSET ?
            """
            frame = connection.execute(
                query, parameters + [int(limit), int(offset)]
            ).df()
            if frame.empty:
                count_query = cte + f" SELECT count(*) FROM ranked {where}"
                total_rows = int(
                    connection.execute(count_query, parameters).fetchone()[0]
                )
            else:
                total_rows = int(frame["filtered_count"].iloc[0])
                frame = frame.drop(columns="filtered_count")
        finally:
            connection.close()
        frames = self._metadata(publication)
        master_map, sector_map = self._metadata_maps(frames)
        rows = self._decorate_rows(
            frame, master_map=master_map, sector_map=sector_map
        )
        pit_members = int(summary_row[1] or 0)
        clean_valid = int(summary_row[3] or 0)
        summary = {
            "observation_date": selected,
            "pit_member_count": pit_members,
            "raw_valid_count": int(summary_row[2] or 0),
            "clean_valid_count": clean_valid,
            "eligible_count": clean_valid,
            "coverage": clean_valid / pit_members if pit_members else 0.0,
            "publication_status": "PUBLISHED",
            "publication_mode": "FACTOR_DATA",
            "requested_date": observation_date,
        }
        self._assert_current(contract)
        return FactorSnapshotResult(
            contract=contract,
            summary=summary,
            rows=rows,
            total_rows=total_rows,
            generation_total_rows=int(summary_row[0] or 0),
            offset=int(offset),
            limit=int(limit),
            previous_date=previous,
            next_date=following,
        )

    @staticmethod
    def _resolve_security(
        frames: dict[str, pd.DataFrame],
        ticker: str,
        *,
        start: str,
        end: str,
    ) -> str:
        symbol = canonical_ticker(ticker)
        symbols = frames.get("symbols", pd.DataFrame()).copy()
        matches = pd.DataFrame()
        if not symbols.empty:
            symbols["ticker"] = symbols["ticker"].astype(str).str.upper()
            starts = pd.to_datetime(symbols["effective_from"], errors="coerce")
            ends = pd.to_datetime(symbols["effective_to"], errors="coerce")
            matches = symbols.loc[
                symbols["ticker"].eq(symbol)
                & (starts.isna() | starts.le(pd.Timestamp(end)))
                & (ends.isna() | ends.ge(pd.Timestamp(start)))
            ]
        ids = sorted(matches["security_id"].astype(str).unique()) if not matches.empty else []
        if not ids:
            master = frames["master"].copy()
            current = master["current_ticker"].astype(str).str.upper()
            ids = sorted(
                master.loc[current.eq(symbol), "security_id"].astype(str).unique()
            )
        if not ids:
            raise FactorObservationError(
                "SECURITY_NOT_FOUND",
                f"Security Master 中找不到股票 {symbol}。",
                status_code=404,
            )
        if len(ids) != 1:
            raise FactorObservationError(
                "SECURITY_ID_AMBIGUOUS",
                f"股票代码 {symbol} 在所选日期范围内对应多个证券身份。",
                status_code=409,
                details={"security_ids": ids},
            )
        return ids[0]

    def history(
        self,
        *,
        factor_id: str,
        ticker: str,
        start: str | None = None,
        end: str | None = None,
    ) -> FactorHistoryResult:
        factor_id = self._factor_id(factor_id)
        try:
            ticker = canonical_ticker(ticker)
        except InvalidResourceId as exc:
            raise FactorObservationError(
                "SECURITY_NOT_FOUND", f"股票代码无效：{ticker}", status_code=404
            ) from exc
        publication, manifest = self._publication(factor_id)
        contract = self._contract(publication, manifest)
        request_start = _date_text(start or contract.date_start, field="start")
        request_end = _date_text(end or contract.date_end, field="end")
        if request_start > request_end:
            raise FactorObservationError(
                "INVALID_QUERY", "开始日期不能晚于结束日期。", status_code=400
            )
        frames = self._metadata(publication)
        security_id = self._resolve_security(
            frames, ticker, start=request_start, end=request_end
        )
        _, paths = self._entries(
            publication, factor_id, start=request_start, end=request_end
        )
        cte = self._ranked_cte(contract.direction)
        connection = self._connect()
        try:
            frame = connection.execute(
                cte
                + """
                    SELECT * FROM ranked
                    WHERE security_id = ?
                    ORDER BY date
                """,
                [
                    [str(path) for path in paths],
                    pd.Timestamp(request_start).date(),
                    pd.Timestamp(request_end).date(),
                    security_id,
                ],
            ).df()
        finally:
            connection.close()
        if frame.empty:
            raise FactorObservationError(
                "TICKER_NOT_IN_GENERATION",
                f"{ticker} 在当前宽基 {factor_id} generation 中没有观测。",
                status_code=404,
                details={"security_id": security_id},
            )
        if len(frame) > MAX_HISTORY_SESSIONS:
            raise FactorObservationError(
                "INVALID_QUERY",
                f"历史查询最多返回 {MAX_HISTORY_SESSIONS} 个交易日。",
                status_code=400,
            )
        master_map, sector_map = self._metadata_maps(frames)
        rows = self._decorate_rows(
            frame, master_map=master_map, sector_map=sector_map
        )
        ranked_rows = [row for row in rows if row["factor_rank"] is not None]
        pit_days = sum(1 for row in rows if row["pit_member"])
        latest_valid = ranked_rows[-1] if ranked_rows else None
        metadata = master_map.get(security_id, {})
        summary = {
            "requested_end": request_end,
            "latest_row_date": rows[-1]["date"],
            "latest_valid_observation_date": latest_valid["date"] if latest_valid else None,
            "latest_valid": latest_valid,
            "valid_sessions": len(ranked_rows),
            "pit_member_sessions": pit_days,
            "total_sessions": len(rows),
            "coverage": len(ranked_rows) / pit_days if pit_days else 0.0,
        }
        self._assert_current(contract)
        return FactorHistoryResult(
            contract=contract,
            ticker=ticker,
            name=str(metadata.get("name") or ticker),
            sector=sector_map.get(security_id, UNKNOWN_CLASSIFICATION),
            request_start=request_start,
            request_end=request_end,
            actual_start=rows[0]["date"],
            actual_end=rows[-1]["date"],
            summary=summary,
            rows=rows,
            security_id=security_id,
        )


__all__ = ["BroadFactorObservationBackend"]
