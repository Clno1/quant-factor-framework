"""Version-bound access to published single-factor observations.

The explorer is deliberately read-only.  It derives rank and percentile from
the verified clean matrix and the dataset version's PIT membership without
persisting another copy of those values.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import math
from pathlib import Path
import threading
from typing import Any, Callable

import numpy as np
import pandas as pd

from src.data.foundation import (
    DataFoundationError,
    MarketDataReader,
)
from src.data.pit import build_membership_mask
from src.factors import artifacts as factor_artifacts
from src.factors import publication as factor_publication
from src.factors.library import get_factor_catalog
from src.research_universes.models import FactorPublicationMode, MembershipType
from src.research_universes.registry import (
    ResearchUniverseRegistry,
    ResearchUniverseRegistryError,
    research_universe_registry,
)
from src.utils.identifiers import (
    InvalidResourceId,
    canonical_ticker,
    safe_path_component,
)
from src.utils.io import load_json
from src.utils.market_calendar import latest_publishable_xnys_session


OBSERVATION_SCHEMA_VERSION = 1
MAX_HISTORY_SESSIONS = 3000
_SNAPSHOT_STATUS_FILTERS = {
    "all",
    "valid",
    "not_pit_member",
    "raw_missing",
    "clean_missing",
    "calculation_window_insufficient",
}
_SNAPSHOT_SORTS = {
    "rank": "factor_rank",
    "ticker": "ticker",
    "raw": "raw_value",
    "clean": "clean_value",
    "percentile": "factor_percentile",
}


class FactorObservationError(RuntimeError):
    """Expected fail-closed query outcome with an API-safe business code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = int(status_code)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }


@dataclass(frozen=True)
class FactorObservationContract:
    schema_version: int
    universe: str
    factor_id: str
    factor_name: str
    direction: int
    publication_id: str
    publication_target_session: str
    factor_generation_id: str
    factor_manifest_sha256: str
    dataset_version_id: str
    dataset_manifest_sha256: str
    membership_sha256: str | None
    membership_type: str
    date_start: str
    date_end: str
    classification_policy: str | None
    publication_mode: str = "FULL_RESEARCH"
    parent_dataset_version_id: str | None = None
    universe_version_id: str | None = None
    normalization_universe_id: str | None = None
    eligibility_sha256: str | None = None
    security_master_generation_id: str | None = None
    security_master_sha256: str | None = None
    preprocessing_methodology_version: str | None = None

    def to_dict(self, **extra: Any) -> dict[str, Any]:
        return {**asdict(self), **extra}


@dataclass(frozen=True)
class FactorSnapshotResult:
    contract: FactorObservationContract
    summary: dict[str, Any]
    rows: list[dict[str, Any]]
    total_rows: int
    generation_total_rows: int
    offset: int
    limit: int
    previous_date: str | None
    next_date: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract.to_dict(
                observation_date=self.summary["observation_date"]
            ),
            "summary": self.summary,
            "rows": self.rows,
            "total_rows": self.total_rows,
            "generation_total_rows": self.generation_total_rows,
            "offset": self.offset,
            "limit": self.limit,
            "previous_date": self.previous_date,
            "next_date": self.next_date,
        }


@dataclass(frozen=True)
class FactorHistoryResult:
    contract: FactorObservationContract
    ticker: str
    name: str
    sector: str
    request_start: str
    request_end: str
    actual_start: str
    actual_end: str
    summary: dict[str, Any]
    rows: list[dict[str, Any]]
    security_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract.to_dict(
                ticker=self.ticker,
                request_start=self.request_start,
                request_end=self.request_end,
            ),
            "ticker": self.ticker,
            "name": self.name,
            "sector": self.sector,
            "security_id": self.security_id,
            "request_start": self.request_start,
            "request_end": self.request_end,
            "actual_start": self.actual_start,
            "actual_end": self.actual_end,
            "summary": self.summary,
            "rows": self.rows,
        }


@dataclass
class _ObservationBundle:
    contract: FactorObservationContract
    raw: pd.DataFrame
    clean: pd.DataFrame
    membership_mask: pd.DataFrame
    ranks: pd.DataFrame
    percentiles: pd.DataFrame
    eligible_counts: pd.Series
    metadata: pd.DataFrame
    first_raw_dates: dict[str, pd.Timestamp | None]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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
            "INVALID_QUERY",
            f"{field} 不是有效日期：{value!r}",
            status_code=400,
        )
    return parsed.date().isoformat()


class FactorObservationReader:
    """Read one immutable factor generation under its publication contract."""

    def __init__(
        self,
        *,
        market_reader: MarketDataReader | None = None,
        registry: ResearchUniverseRegistry | None = None,
        expected_session: str | date | None = None,
        expected_session_provider: Callable[[], str] | None = None,
        broad_backend: Any | None = None,
        cache_size: int = 12,
    ):
        self.market_reader = market_reader or MarketDataReader()
        self.registry = registry or research_universe_registry()
        self._fixed_expected_session = (
            _date_text(expected_session, field="expected_session")
            if expected_session is not None
            else None
        )
        self._expected_session_provider = expected_session_provider
        self._broad_backend = broad_backend
        self._cache_size = max(1, int(cache_size))
        self._cache: dict[tuple[str, ...], _ObservationBundle] = {}
        self._cache_lock = threading.RLock()

    def _broad(self) -> Any:
        if self._broad_backend is None:
            # Imported lazily because the broad adapter returns the domain
            # result classes defined in this module.
            from src.factors.broad_observations import (
                BroadFactorObservationBackend,
            )

            self._broad_backend = BroadFactorObservationBackend(
                expected_session_provider=self._expected_session,
            )
        return self._broad_backend

    def _publication_mode(self, universe: str) -> FactorPublicationMode:
        return self.registry.get(universe).factor_publication_mode

    def _expected_session(self) -> str:
        if self._fixed_expected_session is not None:
            return self._fixed_expected_session
        if self._expected_session_provider is not None:
            return _date_text(
                self._expected_session_provider(), field="expected_session"
            )
        return latest_publishable_xnys_session().date().isoformat()

    def _normalize_universe(self, universe: str) -> str:
        try:
            normalized = safe_path_component(
                str(universe).upper(), label="research_universe"
            )
            self.registry.get(normalized)
            return normalized
        except (InvalidResourceId, ResearchUniverseRegistryError) as exc:
            raise FactorObservationError(
                "INVALID_QUERY",
                f"不支持的研究股票池：{universe}",
                status_code=400,
            ) from exc

    @staticmethod
    def _normalize_factor(factor_id: str) -> str:
        try:
            normalized = safe_path_component(
                str(factor_id).upper(), label="factor_id"
            )
        except InvalidResourceId as exc:
            raise FactorObservationError(
                "FACTOR_NOT_FOUND",
                f"因子不存在：{factor_id}",
                status_code=404,
            ) from exc
        if normalized not in get_factor_catalog():
            raise FactorObservationError(
                "FACTOR_NOT_FOUND",
                f"因子不存在：{normalized}",
                status_code=404,
            )
        return normalized

    def _read_publication(self, universe: str) -> dict[str, Any]:
        path = factor_publication.research_publication_path(universe)
        if not path.exists():
            raise FactorObservationError(
                "RESEARCH_NOT_PUBLISHED",
                f"{universe} 尚无正式因子研究发布。",
                status_code=409,
                details={"universe": universe},
            )
        try:
            publication = load_json(path)
        except Exception as exc:  # noqa: BLE001
            raise FactorObservationError(
                "RESEARCH_INVALID",
                f"{universe} 研究发布文件无法读取，需要重新发布。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        if (
            not isinstance(publication, dict)
            or publication.get("schema_version")
            != factor_publication.RESEARCH_PUBLICATION_SCHEMA_VERSION
            or publication.get("status") != "PUBLISHED"
            or publication.get("universe") != universe
            or not publication.get("publication_id")
        ):
            raise FactorObservationError(
                "RESEARCH_INVALID",
                f"{universe} 研究发布合同不完整，需要按当前版本重新发布。",
                status_code=409,
                details={"path": str(path)},
            )
        data = publication.get("data_foundation")
        if not isinstance(data, dict) or not data.get("version_id"):
            raise FactorObservationError(
                "RESEARCH_INVALID",
                f"{universe} 研究发布未绑定有效行情版本。",
                status_code=409,
            )
        target_session = str(data.get("target_session") or "")
        expected = self._expected_session()
        if target_session != expected:
            raise FactorObservationError(
                "RESEARCH_STALE",
                f"{universe} 研究截止到 {target_session or '未知日期'}，"
                f"当前应发布到 {expected}。",
                status_code=409,
                details={
                    "target_session": target_session or None,
                    "expected_session": expected,
                },
            )
        return publication

    @staticmethod
    def _factor_binding(
        publication: dict[str, Any], factor_id: str
    ) -> dict[str, Any]:
        factors = publication.get("factors")
        binding = factors.get(factor_id) if isinstance(factors, dict) else None
        if (
            not isinstance(binding, dict)
            or not binding.get("generation_id")
            or not binding.get("manifest_sha256")
        ):
            raise FactorObservationError(
                "FACTOR_NOT_FOUND",
                f"当前正式研究发布不包含因子 {factor_id}。",
                status_code=404,
            )
        return binding

    @staticmethod
    def _cache_key(
        universe: str,
        factor_id: str,
        publication: dict[str, Any],
        binding: dict[str, Any],
    ) -> tuple[str, ...]:
        data = publication["data_foundation"]
        return (
            universe,
            str(publication["publication_id"]),
            factor_id,
            str(binding["generation_id"]),
            str(binding["manifest_sha256"]),
            str(data["version_id"]),
        )

    @staticmethod
    def _normalize_matrix(
        frame: pd.DataFrame,
        *,
        label: str,
    ) -> pd.DataFrame:
        if frame.empty:
            raise FactorObservationError(
                "RESEARCH_INVALID",
                f"{label} 因子矩阵为空。",
                status_code=409,
            )
        out = frame.copy()
        out.index = pd.DatetimeIndex(pd.to_datetime(out.index)).normalize()
        out.columns = pd.Index(
            [str(value).strip().upper() for value in out.columns],
            name="ticker",
        )
        if out.index.has_duplicates or out.columns.has_duplicates:
            raise FactorObservationError(
                "RESEARCH_INVALID",
                f"{label} 因子矩阵包含重复日期或股票代码。",
                status_code=409,
            )
        out = out.sort_index()
        out = out.apply(pd.to_numeric, errors="coerce").astype(float)
        return out.where(np.isfinite(out))

    def _load_bundle_once(
        self,
        universe: str,
        factor_id: str,
        publication: dict[str, Any],
        binding: dict[str, Any],
    ) -> _ObservationBundle:
        data = publication["data_foundation"]
        try:
            version = self.market_reader.require_version(
                universe, str(data["version_id"])
            )
            validated = factor_publication.validate_factor_research_publication(
                universe,
                version=version,
                factor_ids=[factor_id],
                publication_id=str(publication["publication_id"]),
            )
        except (DataFoundationError, factor_publication.ResearchPublicationError) as exc:
            code = (
                "PUBLICATION_CHANGED"
                if "publication changed" in str(exc).lower()
                else "RESEARCH_INVALID"
            )
            raise FactorObservationError(
                code,
                (
                    "查询期间研究发布发生变化，请刷新后重试。"
                    if code == "PUBLICATION_CHANGED"
                    else f"{universe} 研究版本完整性校验失败，需要重新发布。"
                ),
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        if validated.get("publication_id") != publication.get("publication_id"):
            raise FactorObservationError(
                "PUBLICATION_CHANGED",
                "查询期间研究发布发生变化，请刷新后重试。",
                status_code=409,
            )

        try:
            raw, clean, manifest = factor_artifacts.load_factor_matrix_bundle(
                factor_id, universe
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            raise FactorObservationError(
                "RESEARCH_INVALID",
                f"{universe}/{factor_id} 的 raw/clean 因子制品校验失败。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc
        manifest_path = factor_artifacts.factor_bundle_manifest_path(
            factor_id, universe
        )
        if (
            manifest.get("generation_id") != binding.get("generation_id")
            or _sha256(manifest_path) != binding.get("manifest_sha256")
        ):
            raise FactorObservationError(
                "PUBLICATION_CHANGED",
                "因子 generation 与研究发布不再一致，请刷新后重试。",
                status_code=409,
            )

        raw = self._normalize_matrix(raw, label="raw")
        clean = self._normalize_matrix(clean, label="clean")
        if not raw.index.equals(clean.index) or not raw.columns.equals(clean.columns):
            raise FactorObservationError(
                "RESEARCH_INVALID",
                "raw 与 clean 因子矩阵未严格对齐。",
                status_code=409,
            )
        date_start = raw.index.min().date().isoformat()
        date_end = raw.index.max().date().isoformat()
        if (
            str(binding.get("date_start") or "") != date_start
            or str(binding.get("date_end") or "") != date_end
            or str(manifest.get("date_start") or "") != date_start
            or str(manifest.get("date_end") or "") != date_end
        ):
            raise FactorObservationError(
                "RESEARCH_INVALID",
                "研究发布记录的日期范围与因子矩阵不一致。",
                status_code=409,
            )

        entry = self.registry.get(universe)
        try:
            membership = self.market_reader.load_membership(
                universe, version=version
            )
            metadata = self.market_reader.load_universe(
                universe, current_only=False, version=version
            )
            if entry.membership_type == MembershipType.PIT:
                if membership is None or membership.empty:
                    raise DataFoundationError(
                        f"[{universe}] formal PIT membership is missing"
                    )
                membership_mask, _diagnostics = build_membership_mask(
                    raw.index,
                    raw.columns,
                    universe,
                    required=True,
                    membership_override=membership,
                    membership_source=(
                        f"duckdb:{version.version_id}:membership"
                    ),
                    membership_source_sha256=(
                        version.membership_checksum_sha256
                    ),
                )
                if membership_mask is None:
                    raise DataFoundationError(
                        f"[{universe}] formal PIT membership produced no mask"
                    )
            else:
                if membership is not None:
                    membership_mask, _diagnostics = build_membership_mask(
                        raw.index,
                        raw.columns,
                        universe,
                        membership_override=membership,
                        membership_source=(
                            f"duckdb:{version.version_id}:membership"
                        ),
                        membership_source_sha256=(
                            version.membership_checksum_sha256
                        ),
                    )
                    if membership_mask is None:
                        membership_mask = pd.DataFrame(
                            True, index=raw.index, columns=raw.columns
                        )
                else:
                    membership_mask = pd.DataFrame(
                        True, index=raw.index, columns=raw.columns
                    )
        except (DataFoundationError, FileNotFoundError, ValueError) as exc:
            raise FactorObservationError(
                "RESEARCH_INVALID",
                f"{universe} 的正式证券信息或 PIT 成分无法通过校验。",
                status_code=409,
                details={"reason": str(exc)},
            ) from exc

        catalog_entry = get_factor_catalog()[factor_id]
        direction = int(catalog_entry.direction)
        manifest_direction = (
            (manifest.get("provenance") or {}).get("factor_direction")
        )
        if manifest_direction != direction:
            raise FactorObservationError(
                "RESEARCH_INVALID",
                f"{factor_id} 的预设方向与 generation manifest 不一致。",
                status_code=409,
            )

        eligible = membership_mask & clean.notna()
        oriented = clean.mul(direction).where(eligible)
        ranks = oriented.rank(axis=1, method="min", ascending=False)
        percentiles = (
            oriented.rank(axis=1, method="average", ascending=True, pct=True)
            * 100.0
        )
        eligible_counts = eligible.sum(axis=1).astype(int)
        raw_in_membership = raw.where(membership_mask)
        first_raw_dates: dict[str, pd.Timestamp | None] = {}
        for ticker in raw.columns:
            first = raw_in_membership[ticker].first_valid_index()
            first_raw_dates[ticker] = (
                pd.Timestamp(first).normalize() if first is not None else None
            )

        metadata = metadata.copy()
        if "ticker" not in metadata.columns:
            raise FactorObservationError(
                "RESEARCH_INVALID",
                "正式证券信息缺少 ticker 列。",
                status_code=409,
            )
        metadata["ticker"] = metadata["ticker"].astype(str).str.upper()
        metadata = metadata.drop_duplicates("ticker", keep="last").set_index(
            "ticker", drop=False
        )
        classification_policy = None
        if "classification_policy" in metadata.columns:
            policies = [
                str(value)
                for value in metadata["classification_policy"].dropna().unique()
                if str(value).strip()
            ]
            if len(policies) == 1:
                classification_policy = policies[0]

        contract = FactorObservationContract(
            schema_version=OBSERVATION_SCHEMA_VERSION,
            universe=universe,
            factor_id=factor_id,
            factor_name=catalog_entry.display_name,
            direction=direction,
            publication_id=str(publication["publication_id"]),
            publication_target_session=str(data["target_session"]),
            factor_generation_id=str(binding["generation_id"]),
            factor_manifest_sha256=str(binding["manifest_sha256"]),
            dataset_version_id=version.version_id,
            dataset_manifest_sha256=str(version.manifest_checksum_sha256),
            membership_sha256=version.membership_checksum_sha256,
            membership_type=entry.membership_type.value,
            date_start=date_start,
            date_end=date_end,
            classification_policy=classification_policy,
            normalization_universe_id=universe,
        )
        return _ObservationBundle(
            contract=contract,
            raw=raw,
            clean=clean,
            membership_mask=membership_mask,
            ranks=ranks,
            percentiles=percentiles,
            eligible_counts=eligible_counts,
            metadata=metadata,
            first_raw_dates=first_raw_dates,
        )

    def _bundle(self, universe: str, factor_id: str) -> _ObservationBundle:
        publication = self._read_publication(universe)
        binding = self._factor_binding(publication, factor_id)
        key = self._cache_key(universe, factor_id, publication, binding)
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        bundle = self._load_bundle_once(
            universe, factor_id, publication, binding
        )
        self._assert_publication_current(bundle.contract)
        with self._cache_lock:
            self._cache[key] = bundle
            while len(self._cache) > self._cache_size:
                self._cache.pop(next(iter(self._cache)))
        return bundle

    def _ticker_alternatives(
        self,
        ticker: str,
        factor_id: str,
        *,
        exclude_universe: str,
    ) -> list[dict[str, Any]]:
        """Locate a ticker in other verified research generations."""
        alternatives: list[dict[str, Any]] = []
        for entry in self.registry.full_research_entries():
            universe_id = entry.universe_id
            if universe_id == exclude_universe:
                continue
            try:
                bundle = self._bundle(universe_id, factor_id)
            except FactorObservationError:
                continue
            if ticker not in bundle.raw.columns:
                continue
            member = bundle.membership_mask[ticker].fillna(False).astype(bool)
            member_dates = bundle.raw.index[member]
            valid = member & bundle.clean[ticker].notna()
            valid_dates = bundle.raw.index[valid]
            alternatives.append(
                {
                    "universe_id": universe_id,
                    "role": entry.role.value,
                    "first_pit_member_date": (
                        pd.Timestamp(member_dates[0]).date().isoformat()
                        if len(member_dates)
                        else None
                    ),
                    "last_pit_member_date": (
                        pd.Timestamp(member_dates[-1]).date().isoformat()
                        if len(member_dates)
                        else None
                    ),
                    "latest_valid_observation_date": (
                        pd.Timestamp(valid_dates[-1]).date().isoformat()
                        if len(valid_dates)
                        else None
                    ),
                    "current_member": bool(member.iloc[-1]),
                }
            )
        return alternatives

    @staticmethod
    def _row_identity(publication: dict[str, Any], factor_id: str) -> tuple[Any, ...]:
        factors = publication.get("factors") or {}
        binding = factors.get(factor_id) or {}
        data = publication.get("data_foundation") or {}
        return (
            publication.get("publication_id"),
            binding.get("generation_id"),
            binding.get("manifest_sha256"),
            data.get("version_id"),
        )

    def _assert_publication_current(
        self, contract: FactorObservationContract
    ) -> None:
        path = factor_publication.research_publication_path(contract.universe)
        try:
            current = load_json(path)
        except Exception as exc:  # noqa: BLE001
            raise FactorObservationError(
                "PUBLICATION_CHANGED",
                "查询期间研究发布发生变化，请刷新后重试。",
                status_code=409,
            ) from exc
        expected = (
            contract.publication_id,
            contract.factor_generation_id,
            contract.factor_manifest_sha256,
            contract.dataset_version_id,
        )
        if self._row_identity(current, contract.factor_id) != expected:
            raise FactorObservationError(
                "PUBLICATION_CHANGED",
                "查询期间研究发布发生变化，请刷新后重试。",
                status_code=409,
            )

    def _with_publication_retry(self, query: Callable[[], Any]) -> Any:
        for attempt in range(2):
            try:
                return query()
            except FactorObservationError as exc:
                if exc.code != "PUBLICATION_CHANGED" or attempt == 1:
                    raise
        raise AssertionError("unreachable")

    @staticmethod
    def _metadata_row(
        bundle: _ObservationBundle, ticker: str
    ) -> tuple[str, str]:
        if ticker not in bundle.metadata.index:
            return ticker, "UNKNOWN"
        row = bundle.metadata.loc[ticker]
        name = str(row.get("name") or ticker)
        sector = str(row.get("sector") or "UNKNOWN")
        return name, sector

    @staticmethod
    def _observation_status(
        bundle: _ObservationBundle,
        observation_date: pd.Timestamp,
        ticker: str,
    ) -> str:
        if not bool(bundle.membership_mask.at[observation_date, ticker]):
            return "NOT_PIT_MEMBER"
        raw_value = _finite_number(bundle.raw.at[observation_date, ticker])
        if raw_value is None:
            first = bundle.first_raw_dates.get(ticker)
            if first is not None and observation_date < first:
                return "CALCULATION_WINDOW_INSUFFICIENT"
            return "RAW_MISSING"
        if _finite_number(bundle.clean.at[observation_date, ticker]) is None:
            return "CLEAN_MISSING"
        return "VALID"

    @staticmethod
    def _quintile(percentile: float | None) -> str | None:
        if percentile is None:
            return None
        bucket = min(5, max(1, int(math.ceil(percentile / 20.0))))
        return f"Q{bucket}"

    def _row(
        self,
        bundle: _ObservationBundle,
        observation_date: pd.Timestamp,
        ticker: str,
    ) -> dict[str, Any]:
        raw_value = _finite_number(bundle.raw.at[observation_date, ticker])
        clean_value = _finite_number(bundle.clean.at[observation_date, ticker])
        rank_value = _finite_number(bundle.ranks.at[observation_date, ticker])
        percentile = _finite_number(
            bundle.percentiles.at[observation_date, ticker]
        )
        eligible_count = int(bundle.eligible_counts.at[observation_date])
        name, sector = self._metadata_row(bundle, ticker)
        return {
            "date": observation_date.date().isoformat(),
            "ticker": ticker,
            "name": name,
            "sector": sector,
            "raw_value": raw_value,
            "clean_value": clean_value,
            "oriented_value": (
                clean_value * bundle.contract.direction
                if clean_value is not None
                and bool(bundle.membership_mask.at[observation_date, ticker])
                else None
            ),
            "factor_rank": int(rank_value) if rank_value is not None else None,
            "eligible_count": eligible_count,
            "factor_percentile": percentile,
            "quintile": self._quintile(percentile),
            "pit_member": bool(
                bundle.membership_mask.at[observation_date, ticker]
            ),
            "status": self._observation_status(
                bundle, observation_date, ticker
            ),
        }

    @staticmethod
    def _resolve_snapshot_date(
        bundle: _ObservationBundle, requested: str
    ) -> tuple[pd.Timestamp, str | None, str | None]:
        dates = bundle.raw.index
        if str(requested).strip().lower() == "latest":
            selected = pd.Timestamp(dates[-1])
        else:
            selected = pd.Timestamp(
                _date_text(requested, field="date")
            ).normalize()
            if selected not in dates:
                position = dates.searchsorted(selected)
                previous_date = (
                    pd.Timestamp(dates[position - 1]).date().isoformat()
                    if position > 0
                    else None
                )
                next_date = (
                    pd.Timestamp(dates[position]).date().isoformat()
                    if position < len(dates)
                    else None
                )
                raise FactorObservationError(
                    "DATE_NOT_AVAILABLE",
                    f"{selected.date()} 没有正式因子观测。",
                    status_code=422,
                    details={
                        "requested_date": selected.date().isoformat(),
                        "previous_date": previous_date,
                        "next_date": next_date,
                    },
                )
        position = int(dates.get_loc(selected))
        previous_date = (
            pd.Timestamp(dates[position - 1]).date().isoformat()
            if position > 0
            else None
        )
        next_date = (
            pd.Timestamp(dates[position + 1]).date().isoformat()
            if position + 1 < len(dates)
            else None
        )
        return selected, previous_date, next_date

    @staticmethod
    def _filter_rows(
        rows: list[dict[str, Any]], status: str, ticker: str | None
    ) -> list[dict[str, Any]]:
        normalized_status = str(status or "all").strip().lower()
        if normalized_status not in _SNAPSHOT_STATUS_FILTERS:
            raise FactorObservationError(
                "INVALID_QUERY",
                f"不支持的状态筛选：{status}",
                status_code=400,
            )
        selected = rows
        if normalized_status == "raw_missing":
            selected = [
                row
                for row in selected
                if row["status"]
                in {"RAW_MISSING", "CALCULATION_WINDOW_INSUFFICIENT"}
            ]
        elif normalized_status != "all":
            expected = normalized_status.upper()
            selected = [row for row in selected if row["status"] == expected]
        query = str(ticker or "").strip().upper()
        if query:
            selected = [
                row
                for row in selected
                if query in row["ticker"] or query in row["name"].upper()
            ]
        return selected

    @staticmethod
    def _sort_rows(
        rows: list[dict[str, Any]], sort: str, order: str
    ) -> list[dict[str, Any]]:
        sort_key = str(sort or "rank").strip().lower()
        if sort_key not in _SNAPSHOT_SORTS:
            raise FactorObservationError(
                "INVALID_QUERY",
                f"不支持的排序字段：{sort}",
                status_code=400,
            )
        normalized_order = str(order or "asc").strip().lower()
        if normalized_order not in {"asc", "desc"}:
            raise FactorObservationError(
                "INVALID_QUERY",
                f"不支持的排序方向：{order}",
                status_code=400,
            )
        field = _SNAPSHOT_SORTS[sort_key]
        present = [row for row in rows if row.get(field) is not None]
        missing = [row for row in rows if row.get(field) is None]
        if field == "ticker":
            present.sort(
                key=lambda row: row["ticker"],
                reverse=normalized_order == "desc",
            )
        else:
            # Python's sort is stable: establish the required ticker-ascending
            # tie order first, then sort only the mathematical value.
            present.sort(key=lambda row: row["ticker"])
            present.sort(
                key=lambda row: row[field],
                reverse=normalized_order == "desc",
            )
        missing.sort(key=lambda row: row["ticker"])
        return present + missing

    def snapshot(
        self,
        *,
        universe: str,
        factor_id: str,
        observation_date: str = "latest",
        ticker: str | None = None,
        status: str = "all",
        sort: str = "rank",
        order: str = "asc",
        offset: int = 0,
        limit: int = 100,
    ) -> FactorSnapshotResult:
        universe = self._normalize_universe(universe)
        factor_id = self._normalize_factor(factor_id)
        mode = self._publication_mode(universe)
        if mode == FactorPublicationMode.FACTOR_DATA:
            return self._broad().snapshot(
                factor_id=factor_id,
                observation_date=observation_date,
                ticker=ticker,
                status=status,
                sort=sort,
                order=order,
                offset=offset,
                limit=limit,
            )
        if mode != FactorPublicationMode.FULL_RESEARCH:
            raise FactorObservationError(
                "UNIVERSE_NOT_QUERYABLE",
                f"{universe} 只提供行情覆盖，不发布 clean 或排名。",
                status_code=422,
            )
        if int(offset) < 0 or not 1 <= int(limit) <= 5000:
            raise FactorObservationError(
                "INVALID_QUERY",
                "offset 必须不小于 0，limit 必须在 1 到 5000 之间。",
                status_code=400,
            )

        def query() -> FactorSnapshotResult:
            bundle = self._bundle(universe, factor_id)
            selected, previous_date, next_date = self._resolve_snapshot_date(
                bundle, observation_date
            )
            rows = [
                self._row(bundle, selected, ticker_code)
                for ticker_code in bundle.raw.columns
            ]
            pit_members = bundle.membership_mask.loc[selected]
            raw_valid = bundle.raw.loc[selected].notna() & pit_members
            clean_valid = bundle.clean.loc[selected].notna() & pit_members
            eligible_count = int(bundle.eligible_counts.at[selected])
            summary = {
                "observation_date": selected.date().isoformat(),
                "pit_member_count": int(pit_members.sum()),
                "raw_valid_count": int(raw_valid.sum()),
                "clean_valid_count": int(clean_valid.sum()),
                "eligible_count": eligible_count,
                "coverage": (
                    float(eligible_count / int(pit_members.sum()))
                    if int(pit_members.sum())
                    else 0.0
                ),
                "publication_status": "PUBLISHED",
                "requested_date": observation_date,
            }
            filtered = self._filter_rows(rows, status, ticker)
            ordered = self._sort_rows(filtered, sort, order)
            self._assert_publication_current(bundle.contract)
            return FactorSnapshotResult(
                contract=bundle.contract,
                summary=summary,
                rows=ordered[int(offset) : int(offset) + int(limit)],
                total_rows=len(ordered),
                generation_total_rows=len(rows),
                offset=int(offset),
                limit=int(limit),
                previous_date=previous_date,
                next_date=next_date,
            )

        return self._with_publication_retry(query)

    def history(
        self,
        *,
        universe: str,
        factor_id: str,
        ticker: str,
        start: str | None = None,
        end: str | None = None,
    ) -> FactorHistoryResult:
        universe = self._normalize_universe(universe)
        factor_id = self._normalize_factor(factor_id)
        mode = self._publication_mode(universe)
        if mode == FactorPublicationMode.FACTOR_DATA:
            return self._broad().history(
                factor_id=factor_id,
                ticker=ticker,
                start=start,
                end=end,
            )
        if mode != FactorPublicationMode.FULL_RESEARCH:
            raise FactorObservationError(
                "UNIVERSE_NOT_QUERYABLE",
                f"{universe} 只提供行情覆盖，不发布 clean 或排名。",
                status_code=422,
            )
        try:
            ticker = canonical_ticker(ticker)
        except InvalidResourceId as exc:
            raise FactorObservationError(
                "TICKER_NOT_IN_GENERATION",
                f"股票代码无效或不在当前正式因子数据范围内：{ticker}",
                status_code=404,
            ) from exc

        def query() -> FactorHistoryResult:
            bundle = self._bundle(universe, factor_id)
            if ticker not in bundle.raw.columns:
                alternatives = self._ticker_alternatives(
                    ticker,
                    factor_id,
                    exclude_universe=universe,
                )
                raise FactorObservationError(
                    "TICKER_NOT_IN_GENERATION",
                    f"{ticker} 不在 {universe} 的当前正式 {factor_id} 因子数据范围内。",
                    status_code=404,
                    details={
                        "ticker": ticker,
                        "selected_universe": universe,
                        "available_universes": alternatives,
                    },
                )
            request_start = _date_text(
                start or bundle.contract.date_start, field="start"
            )
            request_end = _date_text(
                end or bundle.contract.date_end, field="end"
            )
            if request_start > request_end:
                raise FactorObservationError(
                    "INVALID_QUERY",
                    "开始日期不能晚于结束日期。",
                    status_code=400,
                )
            selected_dates = bundle.raw.index[
                (bundle.raw.index >= pd.Timestamp(request_start))
                & (bundle.raw.index <= pd.Timestamp(request_end))
            ]
            if selected_dates.empty:
                raise FactorObservationError(
                    "DATE_NOT_AVAILABLE",
                    "请求区间与当前 factor generation 没有交集。",
                    status_code=422,
                    details={
                        "generation_start": bundle.contract.date_start,
                        "generation_end": bundle.contract.date_end,
                    },
                )
            if len(selected_dates) > MAX_HISTORY_SESSIONS:
                raise FactorObservationError(
                    "INVALID_QUERY",
                    f"历史查询最多返回 {MAX_HISTORY_SESSIONS} 个交易日。",
                    status_code=400,
                )
            rows = [
                self._row(bundle, pd.Timestamp(value), ticker)
                for value in selected_dates
            ]
            valid_rows = [row for row in rows if row["status"] == "VALID"]
            pit_days = sum(1 for row in rows if row["pit_member"])
            latest_valid = valid_rows[-1] if valid_rows else None
            summary = {
                "requested_end": request_end,
                "latest_row_date": rows[-1]["date"],
                "latest_valid_observation_date": (
                    latest_valid["date"] if latest_valid else None
                ),
                "latest_valid": latest_valid,
                "valid_sessions": len(valid_rows),
                "pit_member_sessions": pit_days,
                "total_sessions": len(rows),
                "coverage": (
                    float(len(valid_rows) / pit_days) if pit_days else 0.0
                ),
            }
            name, sector = self._metadata_row(bundle, ticker)
            self._assert_publication_current(bundle.contract)
            return FactorHistoryResult(
                contract=bundle.contract,
                ticker=ticker,
                name=name,
                sector=sector,
                request_start=request_start,
                request_end=request_end,
                actual_start=rows[0]["date"],
                actual_end=rows[-1]["date"],
                summary=summary,
                rows=rows,
            )

        return self._with_publication_retry(query)

    def search_securities(
        self,
        *,
        query: str,
        asof: str | date | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search the dated Security Master, independent of matrix columns."""
        return self._broad().search_securities(
            query=query,
            asof=asof,
            limit=limit,
        )

    def metadata(
        self,
        *,
        selected_universe: str | None = None,
        selected_factor: str | None = None,
    ) -> dict[str, Any]:
        selected_universe = (
            self._normalize_universe(selected_universe)
            if selected_universe
            else None
        )
        selected_factor = (
            self._normalize_factor(selected_factor)
            if selected_factor
            else None
        )
        catalog = get_factor_catalog()
        universe_rows: list[dict[str, Any]] = []
        available_dates: list[str] = []
        ticker_options: list[dict[str, str]] = []
        for entry in self.registry.factor_data_entries():
            universe_id = entry.universe_id
            row: dict[str, Any] = {
                **entry.to_dict(),
                "status": "MISSING",
                "factor_data_status": "MISSING",
                "research_status": "MISSING",
                "publication_id": None,
                "publication_target_session": None,
                "dataset_version_id": None,
                "parent_dataset_version_id": None,
                "universe_version_id": None,
                "capabilities": {
                    "raw": False,
                    "clean": False,
                    "rank": False,
                    "confidence": False,
                },
                "factors": [],
                "error": None,
            }
            try:
                if entry.factor_publication_mode == FactorPublicationMode.FACTOR_DATA:
                    broad = self._broad().metadata(selected_factor=selected_factor)
                    row.update(broad["universe"])
                    if selected_universe == universe_id:
                        available_dates = broad["available_dates"]
                        ticker_options = broad.get("ticker_options") or []
                    universe_rows.append(row)
                    continue
                publication = self._read_publication(universe_id)
                factor_bindings = publication.get("factors") or {}
                data = publication["data_foundation"]
                version = self.market_reader.require_version(
                    universe_id, str(data["version_id"])
                )
                factor_publication.validate_factor_research_publication(
                    universe_id,
                    version=version,
                    factor_ids=list(factor_bindings),
                    publication_id=str(publication["publication_id"]),
                )
                row.update(
                    {
                        "status": "PUBLISHED",
                        "factor_data_status": "PUBLISHED",
                        "research_status": "PUBLISHED",
                        "publication_id": publication["publication_id"],
                        "publication_target_session": data["target_session"],
                        "dataset_version_id": data["version_id"],
                        "parent_dataset_version_id": data["version_id"],
                        "capabilities": {
                            "raw": True,
                            "clean": True,
                            "rank": True,
                            "confidence": bool(entry.confidence_enabled),
                        },
                        "factors": [
                            {
                                "factor_id": factor_id,
                                "display_name": catalog[factor_id].display_name,
                                "direction": catalog[factor_id].direction,
                                "date_start": binding.get("date_start"),
                                "date_end": binding.get("date_end"),
                                "generation_id": binding.get("generation_id"),
                            }
                            for factor_id, binding in factor_bindings.items()
                            if factor_id in catalog
                        ],
                    }
                )
                if (
                    selected_universe == universe_id
                    and selected_factor
                    and selected_factor in factor_bindings
                ):
                    bundle = self._bundle(universe_id, selected_factor)
                    available_dates = [
                        pd.Timestamp(value).date().isoformat()
                        for value in bundle.raw.index
                    ]
                    ticker_options = [
                        {
                            "ticker": ticker,
                            "name": self._metadata_row(bundle, ticker)[0],
                        }
                        for ticker in bundle.raw.columns
                    ]
            except FactorObservationError as exc:
                status = {
                    "RESEARCH_NOT_PUBLISHED": "MISSING",
                    "RESEARCH_STALE": "STALE",
                    "FACTOR_DATA_NOT_PUBLISHED": "MISSING",
                    "FACTOR_DATA_STALE": "STALE",
                }.get(exc.code, "INVALID")
                row["status"] = status
                if entry.factor_publication_mode == FactorPublicationMode.FACTOR_DATA:
                    row["factor_data_status"] = status
                else:
                    row["factor_data_status"] = status
                    row["research_status"] = status
                row["error"] = exc.to_dict()
            except (DataFoundationError, factor_publication.ResearchPublicationError) as exc:
                row["status"] = "INVALID"
                row["error"] = {
                    "code": "RESEARCH_INVALID",
                    "message": f"{universe_id} 研究版本完整性校验失败。",
                    "details": {"reason": str(exc)},
                }
            universe_rows.append(row)
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "expected_session": self._expected_session(),
            "universes": universe_rows,
            "factor_catalog": [
                {
                    "factor_id": entry.id,
                    "display_name": entry.display_name,
                    "category": entry.category,
                    "direction": entry.direction,
                }
                for entry in catalog.values()
            ],
            "selected_universe": selected_universe,
            "selected_factor": selected_factor,
            "available_dates": available_dates,
            "ticker_options": ticker_options,
        }


__all__ = [
    "FactorHistoryResult",
    "FactorObservationContract",
    "FactorObservationError",
    "FactorObservationReader",
    "FactorSnapshotResult",
    "MAX_HISTORY_SESSIONS",
    "OBSERVATION_SCHEMA_VERSION",
]
