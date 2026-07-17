"""Classification normalization and Stage-1 security counting units.

This module is deliberately provider-agnostic.  It turns a current
classification payload into a deterministic membership table and applies the
small, versioned set of manually reviewed share-class overrides.  It never
fetches network data and never imports the Web, factor, backtest, or paper
trading domains.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
import yaml

from .models import DedupeStatus, GroupAnalyticsError, ReasonCode, sorted_reason_codes


_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,15}$")
_COUNTING_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
_NON_SLUG = re.compile(r"[^a-z0-9]+")

CLASSIFICATION_HASH_COLUMNS = (
    "security_id",
    "ticker",
    "name",
    "asset_type",
    "taxonomy",
    "level",
    "group_id",
    "group_name",
    "exposure",
    "source",
    "source_version",
    "group_id_mapping_version",
    "delisting_return_required",
)


class ClassificationValidationError(GroupAnalyticsError):
    code = "INVALID_CLASSIFICATION"
    stage = "classification"


class IssuerOverrideValidationError(GroupAnalyticsError):
    code = "INVALID_ISSUER_OVERRIDE"
    stage = "classification"


class GroupIdMappingValidationError(GroupAnalyticsError):
    code = "INVALID_GROUP_ID_MAPPING"
    stage = "classification"


@dataclass(frozen=True, slots=True)
class StableGroupIdMapping:
    version: str
    taxonomy: str
    provider: str
    groups: Mapping[str, Mapping[str, str]]
    aliases: Mapping[str, Mapping[str, str]]
    source_path: Path
    file_hash: str

    def resolve(self, *, level: str, group_name: str) -> str:
        level = str(level).lower()
        name = _clean_text(group_name)
        if name is None:
            raise GroupIdMappingValidationError("group_name cannot be empty")
        direct = self.groups.get(level, {})
        aliases = self.aliases.get(level, {})
        group_id = direct.get(name) or aliases.get(name)
        if group_id is None:
            raise GroupIdMappingValidationError(
                "FMP classification label is absent from the versioned ID registry",
                details={
                    "level": level,
                    "group_name": name,
                    "mapping_version": self.version,
                },
            )
        return group_id


@dataclass(frozen=True, slots=True)
class IssuerOverride:
    counting_unit_id: str
    tickers: tuple[str, ...]
    fallback_representative_ticker: str
    issuer_name: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None

    def active_on(self, asof: str | date | datetime | pd.Timestamp) -> bool:
        target = _session(asof)
        if self.valid_from and target < _session(self.valid_from):
            return False
        if self.valid_to and target >= _session(self.valid_to):
            return False
        return True


@dataclass(frozen=True, slots=True)
class IssuerOverrideSet:
    version: str
    source: str | None
    overrides: tuple[IssuerOverride, ...]
    schema_version: int = 1
    source_path: Path | None = None
    file_hash: str | None = None

    @classmethod
    def empty(cls, source_path: Path | None = None) -> "IssuerOverrideSet":
        return cls(
            version="NONE",
            source=None,
            overrides=(),
            source_path=source_path,
        )


def normalize_ticker(value: Any) -> str:
    """Normalize repository/FMP ticker spelling and reject unsafe paths."""

    ticker = str(value or "").strip().upper().replace(".", "-")
    if not _TICKER_RE.fullmatch(ticker) or ".." in ticker:
        raise ClassificationValidationError(
            f"Invalid ticker in classification input: {value!r}",
            details={"ticker": str(value)},
        )
    return ticker


def _clean_text(value: Any) -> str | None:
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    text = " ".join(str(value).strip().split())
    return text or None


def _boolean_flag(value: Any, *, field: str) -> bool:
    if value is None or value is pd.NA:
        return False
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n", ""}:
        return False
    raise ClassificationValidationError(
        f"Invalid boolean value for {field}",
        details={"field": field, "value": str(value)},
    )


def _slug(value: str) -> str:
    slug = _NON_SLUG.sub("-", value.casefold()).strip("-")
    if slug:
        return slug
    return "name-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _session(value: str | date | datetime | pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("asof cannot be NaT")
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("America/New_York").tz_localize(None)
    return timestamp.normalize()


def _json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if np.isfinite(number) else None
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def _canonical_frame_bytes(frame: pd.DataFrame, columns: Iterable[str]) -> bytes:
    selected = frame.reindex(columns=list(columns)).copy().reset_index(drop=True)
    sort_columns = list(selected.columns)
    sortable = selected.copy()
    for column in sort_columns:
        sortable[column] = sortable[column].map(
            lambda value: json.dumps(
                _json_value(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    order = sortable.sort_values(sort_columns, kind="mergesort").index
    records = [
        {column: _json_value(value) for column, value in row.items()}
        for row in selected.loc[order].to_dict(orient="records")
    ]
    return json.dumps(
        records,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def classification_hash(frame: pd.DataFrame) -> str:
    """Hash semantic membership columns independent of input row order."""

    missing = set(CLASSIFICATION_HASH_COLUMNS) - set(frame.columns)
    if missing:
        raise ClassificationValidationError(
            "Classification frame is missing canonical columns",
            details={"missing_columns": sorted(missing)},
        )
    return hashlib.sha256(
        _canonical_frame_bytes(frame, CLASSIFICATION_HASH_COLUMNS)
    ).hexdigest()


def _provider_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Coalesce raw/normalized provider aliases without duplicate columns."""

    source = frame.copy()
    aliases = {
        "ticker": ("ticker", "symbol"),
        "name": ("name", "companyName"),
        "sector": ("sector",),
        "sub_industry": ("sub_industry", "subSector", "industry"),
        "asset_type": ("asset_type",),
        "delisting_return_required": (
            "delisting_return_required",
            "requires_delisting_return",
        ),
    }
    for target, candidates in aliases.items():
        if target in source.columns:
            continue
        candidate = next((item for item in candidates if item in source.columns), None)
        if candidate is not None:
            source[target] = source[candidate]
    return source


def source_payload_hash(frame: pd.DataFrame) -> str:
    """Stable hash of the provider payload fields used by Stage 1."""

    source = _provider_columns(frame)
    columns = (
        "ticker",
        "name",
        "sector",
        "sub_industry",
        "delisting_return_required",
    )
    for column in columns:
        if column not in source.columns:
            source[column] = None
    source["ticker"] = source["ticker"].map(normalize_ticker)
    for column in ("name", "sector", "sub_industry"):
        source[column] = source[column].map(_clean_text)
    source["delisting_return_required"] = source[
        "delisting_return_required"
    ].map(lambda value: _boolean_flag(value, field="delisting_return_required"))
    return hashlib.sha256(_canonical_frame_bytes(source, columns)).hexdigest()


def load_stable_group_id_mapping(path: str | Path) -> StableGroupIdMapping:
    """Load the reviewed FMP label-to-ID registry used by formal snapshots."""

    source_path = Path(path)
    try:
        raw_bytes = source_path.read_bytes()
        payload = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise GroupIdMappingValidationError(
            "Unable to read the stable group ID mapping",
            details={"path": str(source_path)},
        ) from exc
    if not isinstance(payload, Mapping):
        raise GroupIdMappingValidationError("group ID mapping root must be an object")
    if int(payload.get("schema_version", 0)) != 1:
        raise GroupIdMappingValidationError("unsupported group ID mapping schema_version")
    version = _clean_text(payload.get("version"))
    taxonomy = str(payload.get("taxonomy") or "").strip().upper()
    provider = str(payload.get("provider") or "").strip().upper()
    if not version or taxonomy != "FMP" or provider != "FMP":
        raise GroupIdMappingValidationError(
            "group ID mapping requires version and FMP taxonomy/provider"
        )
    levels = payload.get("levels")
    if not isinstance(levels, Mapping):
        raise GroupIdMappingValidationError("group ID mapping levels must be an object")

    groups: dict[str, dict[str, str]] = {}
    aliases: dict[str, dict[str, str]] = {}
    for level in ("sector", "sub_industry"):
        raw_level = levels.get(level)
        if not isinstance(raw_level, Mapping):
            raise GroupIdMappingValidationError(f"missing mapping level {level}")
        raw_groups = raw_level.get("groups")
        raw_aliases = raw_level.get("aliases", {})
        if not isinstance(raw_groups, Mapping) or not raw_groups:
            raise GroupIdMappingValidationError(f"{level}.groups must be non-empty")
        if not isinstance(raw_aliases, Mapping):
            raise GroupIdMappingValidationError(f"{level}.aliases must be an object")
        parsed_groups: dict[str, str] = {}
        for raw_name, raw_id in raw_groups.items():
            name = _clean_text(raw_name)
            group_id = _clean_text(raw_id)
            expected_prefix = f"fmp:{level}:"
            if (
                name is None
                or group_id is None
                or not group_id.startswith(expected_prefix)
                or not _COUNTING_UNIT_RE.fullmatch(group_id)
                or ".." in group_id
            ):
                raise GroupIdMappingValidationError(
                    f"invalid {level} group ID mapping",
                    details={"group_name": name, "group_id": group_id},
                )
            parsed_groups[name] = group_id
        if len(set(parsed_groups.values())) != len(parsed_groups):
            raise GroupIdMappingValidationError(
                f"{level} canonical group IDs must be unique"
            )
        parsed_aliases: dict[str, str] = {}
        canonical_ids = set(parsed_groups.values())
        for raw_name, raw_id in raw_aliases.items():
            name = _clean_text(raw_name)
            group_id = _clean_text(raw_id)
            if name is None or group_id not in canonical_ids or name in parsed_groups:
                raise GroupIdMappingValidationError(
                    f"invalid {level} group ID alias",
                    details={"alias": name, "group_id": group_id},
                )
            parsed_aliases[name] = group_id
        groups[level] = parsed_groups
        aliases[level] = parsed_aliases

    return StableGroupIdMapping(
        version=version,
        taxonomy=taxonomy,
        provider=provider,
        groups=groups,
        aliases=aliases,
        source_path=source_path,
        file_hash=hashlib.sha256(raw_bytes).hexdigest(),
    )


def normalize_classification_frame(
    frame: pd.DataFrame,
    *,
    taxonomy: str,
    level: str,
    classification_asof: str,
    fetched_at: str,
    source: str = "FMP",
    source_version: str = "fmp-stable:/sp500-constituent",
    group_id_mapping: StableGroupIdMapping | None = None,
) -> pd.DataFrame:
    """Create one deterministic standard-industry membership row per ticker.

    Missing classifications remain in the result so they stay in the expected
    universe denominator.  Duplicate rows with different groups are rejected;
    a standard-industry security may never be assigned to two groups at one
    level.
    """

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ClassificationValidationError("Classification provider returned no rows")
    taxonomy = str(taxonomy).strip().upper()
    level = str(level).strip().lower()
    if level not in {"sector", "sub_industry"}:
        raise ClassificationValidationError(
            f"Stage 1 does not support classification level {level!r}",
            details={"allowed_levels": ["sector", "sub_industry"]},
        )
    if group_id_mapping is None:
        raise GroupIdMappingValidationError(
            "Formal FMP normalization requires a versioned stable group ID mapping"
        )
    if group_id_mapping.taxonomy != taxonomy or group_id_mapping.provider != str(source).strip().upper():
        raise GroupIdMappingValidationError(
            "group ID mapping does not match normalization taxonomy/provider"
        )

    working = _provider_columns(frame)
    if "ticker" not in working.columns:
        raise ClassificationValidationError("Classification payload is missing ticker")
    for column in (
        "name",
        "sector",
        "sub_industry",
        "asset_type",
        "delisting_return_required",
    ):
        if column not in working.columns:
            working[column] = False if column == "delisting_return_required" else None

    working["ticker"] = working["ticker"].map(normalize_ticker)
    for column in ("name", "sector", "sub_industry", "asset_type"):
        working[column] = working[column].map(_clean_text)
    working["delisting_return_required"] = working[
        "delisting_return_required"
    ].map(lambda value: _boolean_flag(value, field="delisting_return_required"))
    working["group_name"] = working[level]

    conflicts: dict[str, list[str | None]] = {}
    for ticker, rows in working.groupby("ticker", sort=True):
        groups = sorted(
            {_clean_text(value) for value in rows["group_name"]},
            key=lambda value: "" if value is None else value,
        )
        if len(groups) > 1:
            conflicts[str(ticker)] = groups
    if conflicts:
        raise ClassificationValidationError(
            "Standard-industry classification is not mutually exclusive",
            details={"conflicting_tickers": conflicts},
        )

    # Identical provider duplicates do not change membership.  Stable sorting
    # makes the selected display name independent of source row order.
    working = working.sort_values(
        ["ticker", "group_name", "name"],
        na_position="last",
        kind="mergesort",
    ).drop_duplicates("ticker", keep="first")
    working["group_id"] = working["group_name"].map(
        lambda value: (
            group_id_mapping.resolve(level=level, group_name=value)
            if isinstance(value, str) and value
            else None
        )
    )

    classified = working.dropna(subset=["group_id"])
    collisions = (
        classified.groupby("group_id")["group_name"].nunique().loc[lambda value: value > 1]
    )
    if not collisions.empty:
        raise ClassificationValidationError(
            "Two provider group names map to the same stable group_id",
            details={"group_ids": collisions.index.tolist()},
        )

    working["security_id"] = "security:" + working["ticker"]
    working["asset_type"] = working["asset_type"].fillna("STOCK").str.upper()
    working["taxonomy"] = taxonomy
    working["level"] = level
    working["taxonomy_level"] = level
    working["exposure"] = 1.0
    working["primary_flag"] = True
    working["valid_from"] = None
    working["valid_to"] = None
    working["available_at"] = fetched_at
    working["classification_asof"] = classification_asof
    working["source"] = str(source).strip().upper()
    working["source_version"] = (
        f"{source_version};group-id-map={group_id_mapping.version}"
    )
    working["group_id_mapping_version"] = group_id_mapping.version
    working["is_classified"] = working["group_id"].notna()
    working["reason_codes"] = working["is_classified"].map(
        lambda valid: [] if valid else [ReasonCode.MISSING_CLASSIFICATION.value]
    )

    columns = [
        "security_id",
        "ticker",
        "name",
        "asset_type",
        "taxonomy",
        "level",
        "taxonomy_level",
        "group_id",
        "group_name",
        "exposure",
        "primary_flag",
        "valid_from",
        "valid_to",
        "available_at",
        "classification_asof",
        "source",
        "source_version",
        "is_classified",
        "reason_codes",
        "delisting_return_required",
        "group_id_mapping_version",
    ]
    return working[columns].sort_values("ticker", kind="mergesort").reset_index(drop=True)


def load_issuer_overrides(path: str | Path | None) -> IssuerOverrideSet:
    """Load and validate a versioned manual share-class override file.

    A missing file is a legitimate ``NONE`` state.  A present but unversioned,
    overlapping, or malformed file is rejected rather than partially applied.
    """

    if path is None:
        return IssuerOverrideSet.empty()
    source_path = Path(path)
    if not source_path.exists():
        return IssuerOverrideSet.empty(source_path)
    try:
        raw_bytes = source_path.read_bytes()
        payload = yaml.safe_load(raw_bytes.decode("utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise IssuerOverrideValidationError(
            f"Unable to read issuer override file: {source_path}",
            details={"path": str(source_path)},
        ) from exc
    if not isinstance(payload, Mapping):
        raise IssuerOverrideValidationError("Issuer override root must be a mapping")

    version = _clean_text(payload.get("version"))
    source = _clean_text(payload.get("source"))
    schema_version = int(payload.get("schema_version", 1))
    raw_overrides = payload.get("overrides", [])
    if not isinstance(raw_overrides, list):
        raise IssuerOverrideValidationError("issuer overrides must be a list")
    if raw_overrides and not version:
        raise IssuerOverrideValidationError("issuer overrides require a non-empty version")
    if schema_version != 1:
        raise IssuerOverrideValidationError(
            f"Unsupported issuer override schema_version={schema_version}"
        )

    parsed: list[IssuerOverride] = []
    ticker_owner: dict[str, str] = {}
    ids: set[str] = set()
    for index, raw in enumerate(raw_overrides):
        if not isinstance(raw, Mapping):
            raise IssuerOverrideValidationError(
                f"issuer override at index {index} must be a mapping"
            )
        unit_id = _clean_text(raw.get("counting_unit_id"))
        if not unit_id or not _COUNTING_UNIT_RE.fullmatch(unit_id) or ".." in unit_id:
            raise IssuerOverrideValidationError(
                f"Invalid counting_unit_id at override index {index}",
                details={"counting_unit_id": unit_id},
            )
        if unit_id in ids:
            raise IssuerOverrideValidationError(
                f"Duplicate issuer override counting_unit_id: {unit_id}"
            )
        ids.add(unit_id)
        raw_tickers = raw.get("tickers")
        if not isinstance(raw_tickers, list) or not raw_tickers:
            raise IssuerOverrideValidationError(
                f"Issuer override {unit_id} requires a non-empty tickers list"
            )
        tickers = tuple(sorted({normalize_ticker(item) for item in raw_tickers}))
        if len(tickers) != len(raw_tickers):
            raise IssuerOverrideValidationError(
                f"Issuer override {unit_id} contains duplicate tickers"
            )
        for ticker in tickers:
            if ticker in ticker_owner:
                raise IssuerOverrideValidationError(
                    f"Ticker {ticker} belongs to multiple issuer overrides",
                    details={"first": ticker_owner[ticker], "second": unit_id},
                )
            ticker_owner[ticker] = unit_id
        fallback = normalize_ticker(
            raw.get("fallback_representative_ticker") or tickers[0]
        )
        if fallback not in tickers:
            raise IssuerOverrideValidationError(
                f"Fallback representative {fallback} is not in {unit_id} tickers"
            )
        valid_from = _clean_text(raw.get("valid_from"))
        valid_to = _clean_text(raw.get("valid_to"))
        if valid_from and valid_to and _session(valid_from) >= _session(valid_to):
            raise IssuerOverrideValidationError(
                f"Issuer override {unit_id} has an invalid validity interval"
            )
        parsed.append(
            IssuerOverride(
                counting_unit_id=unit_id,
                tickers=tickers,
                fallback_representative_ticker=fallback,
                issuer_name=_clean_text(raw.get("issuer_name")),
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )

    return IssuerOverrideSet(
        version=version or "NONE",
        source=source,
        overrides=tuple(sorted(parsed, key=lambda item: item.counting_unit_id)),
        schema_version=schema_version,
        source_path=source_path,
        file_hash=hashlib.sha256(raw_bytes).hexdigest(),
    )


def _return_series(value: pd.Series | pd.DataFrame | Mapping[str, Any]) -> pd.Series:
    if isinstance(value, pd.DataFrame):
        if "ticker" in value.columns:
            value = value.set_index("ticker", drop=False)
        column = next(
            (
                candidate
                for candidate in ("raw_return_1d", "raw_return", "return")
                if candidate in value.columns
            ),
            None,
        )
        if column is None:
            raise ValueError("security_returns DataFrame has no return column")
        result = value[column]
    elif isinstance(value, pd.Series):
        result = value
    elif isinstance(value, Mapping):
        result = pd.Series(dict(value))
    else:
        raise TypeError("security_returns must be a Series, DataFrame, or mapping")
    result = result.copy()
    result.index = [normalize_ticker(item) for item in result.index]
    if result.index.has_duplicates:
        raise ValueError("security_returns contains duplicate tickers")
    return pd.to_numeric(result, errors="coerce").astype(float)


def _normalize_wide(frame: pd.DataFrame | None) -> pd.DataFrame | None:
    if frame is None:
        return None
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("price, volume, and market-cap inputs must be DataFrames")
    out = frame.copy()
    out.columns = [normalize_ticker(item) for item in out.columns]
    if out.columns.has_duplicates:
        raise ValueError("wide input contains duplicate ticker columns")
    out.index = [_session(item) for item in out.index]
    if out.index.has_duplicates:
        raise ValueError("wide input contains duplicate sessions")
    return out.apply(pd.to_numeric, errors="coerce").sort_index()


def _last_row_before(frame: pd.DataFrame | None, asof: pd.Timestamp) -> tuple[pd.Timestamp | None, pd.Series]:
    if frame is None or frame.empty:
        return None, pd.Series(dtype=float)
    eligible = frame.loc[frame.index < asof]
    if eligible.empty:
        return None, pd.Series(dtype=float)
    session = pd.Timestamp(eligible.index[-1])
    return session, eligible.iloc[-1]


def _adv60(
    price: pd.DataFrame | None,
    volume: pd.DataFrame | None,
    *,
    asof: pd.Timestamp,
    tickers: list[str],
) -> tuple[pd.Series, pd.Timestamp | None, dict[str, int]]:
    if price is None or volume is None or price.empty or volume.empty:
        return pd.Series(np.nan, index=tickers, dtype=float), None, {ticker: 0 for ticker in tickers}
    common_index = price.index.intersection(volume.index)
    common_index = common_index[common_index < asof][-60:]
    if len(common_index) == 0:
        return pd.Series(np.nan, index=tickers, dtype=float), None, {ticker: 0 for ticker in tickers}
    prices = price.reindex(index=common_index, columns=tickers)
    volumes = volume.reindex(index=common_index, columns=tickers)
    dollar_volume = (prices * volumes).where((prices > 0) & (volumes >= 0))
    counts = dollar_volume.notna().sum().astype(int).to_dict()
    values = dollar_volume.mean(axis=0, skipna=True).where(lambda item: item > 0)
    return values.astype(float), pd.Timestamp(common_index[-1]), counts


def _classification_conflict(rows: pd.DataFrame) -> bool:
    keys = {
        (
            _clean_text(row.get("taxonomy")),
            _clean_text(row.get("level") or row.get("taxonomy_level")),
            _clean_text(row.get("group_id")),
            _clean_text(row.get("group_name")),
        )
        for row in rows.to_dict(orient="records")
    }
    return len(keys) > 1


def build_counting_units(
    classification: pd.DataFrame,
    *,
    security_returns: pd.Series | pd.DataFrame | Mapping[str, Any],
    asof: str | date | datetime | pd.Timestamp,
    overrides: IssuerOverrideSet | None = None,
    liquidity_price: pd.DataFrame | None = None,
    volume: pd.DataFrame | None = None,
    market_cap: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build the Stage-1 one-row-per-counting-unit expected frame.

    All representative/security weighting inputs are restricted to sessions
    strictly before ``asof``.  Complete positive share-class market caps take
    precedence.  Otherwise a representative is selected by trailing 60-session
    average dollar volume, with the versioned YAML fallback used only when that
    history is unavailable.  Current-day returns are never used for selection.
    """

    if not isinstance(classification, pd.DataFrame) or classification.empty:
        raise ClassificationValidationError("classification must contain securities")
    required = {"ticker", "security_id", "taxonomy", "group_id", "group_name"}
    missing = required - set(classification.columns)
    if missing:
        raise ClassificationValidationError(
            "classification is missing counting-unit inputs",
            details={"missing_columns": sorted(missing)},
        )
    members = classification.copy()
    members["ticker"] = members["ticker"].map(normalize_ticker)
    if members["ticker"].duplicated().any():
        raise ClassificationValidationError(
            "classification must contain one row per security before issuer overrides"
        )
    members = members.set_index("ticker", drop=False).sort_index()
    returns = _return_series(security_returns)
    target = _session(asof)
    price = _normalize_wide(liquidity_price)
    vol = _normalize_wide(volume)
    caps = _normalize_wide(market_cap)
    override_set = overrides or IssuerOverrideSet.empty()
    active = [item for item in override_set.overrides if item.active_on(target)]

    ticker_to_override: dict[str, IssuerOverride] = {
        ticker: item
        for item in active
        for ticker in item.tickers
        if ticker in members.index
    }
    units: list[tuple[str, IssuerOverride | None, list[str]]] = []
    for ticker in members.index:
        item = ticker_to_override.get(str(ticker))
        if item is None:
            units.append((f"security:{ticker}", None, [str(ticker)]))
        elif not any(unit_id == item.counting_unit_id for unit_id, _, _ in units):
            present = [candidate for candidate in item.tickers if candidate in members.index]
            units.append((item.counting_unit_id, item, present))

    output: list[dict[str, Any]] = []
    override_details: list[dict[str, Any]] = []
    conflicts: list[str] = []
    applied_override_count = 0
    for unit_id, override, tickers in units:
        security_rows = members.loc[tickers]
        if isinstance(security_rows, pd.Series):
            security_rows = security_rows.to_frame().T
        conflict = bool(override is not None and _classification_conflict(security_rows))
        return_values = returns.reindex(tickers)
        member_weights: dict[str, float] = {}
        cap_session: pd.Timestamp | None = None
        adv_session: pd.Timestamp | None = None
        adv_counts: dict[str, int] = {ticker: 0 for ticker in tickers}

        if override is None:
            representative = tickers[0]
            method = "SECURITY"
            member_weights = {representative: 1.0}
            raw_return = return_values.get(representative, np.nan)
        else:
            applied_override_count += 1
            if len(tickers) == 1:
                representative = tickers[0]
                method = "SINGLE_SECURITY_OVERRIDE"
                member_weights = {representative: 1.0}
                raw_return = return_values.get(representative, np.nan)
            else:
                cap_session, cap_row = _last_row_before(caps, target)
                selected_caps = pd.to_numeric(cap_row.reindex(tickers), errors="coerce")
                complete_caps = bool(
                    len(selected_caps) == len(tickers)
                    and selected_caps.notna().all()
                    and (selected_caps > 0).all()
                )
                if complete_caps:
                    weights = selected_caps / selected_caps.sum()
                    member_weights = {ticker: float(weights.at[ticker]) for ticker in tickers}
                    representative = sorted(
                        tickers,
                        key=lambda ticker: (-float(selected_caps.at[ticker]), ticker),
                    )[0]
                    method = "SHARE_CLASS_MARKET_CAP"
                    raw_return = (
                        float((return_values * weights).sum())
                        if return_values.notna().all()
                        else np.nan
                    )
                else:
                    adv, adv_session, adv_counts = _adv60(
                        price,
                        vol,
                        asof=target,
                        tickers=tickers,
                    )
                    available = adv.dropna()
                    if not available.empty:
                        representative = sorted(
                            available.index,
                            key=lambda ticker: (-float(available.at[ticker]), str(ticker)),
                        )[0]
                        method = "REPRESENTATIVE_60D_DOLLAR_VOLUME"
                    else:
                        representative = (
                            override.fallback_representative_ticker
                            if override.fallback_representative_ticker in tickers
                            else sorted(tickers)[0]
                        )
                        method = "VERSIONED_FALLBACK_REPRESENTATIVE"
                    member_weights = {
                        ticker: 1.0 if ticker == representative else 0.0
                        for ticker in tickers
                    }
                    raw_return = return_values.get(representative, np.nan)

        base = security_rows.sort_index(kind="mergesort").iloc[0].to_dict()
        reasons: list[str | ReasonCode] = []
        for value in security_rows.get("reason_codes", pd.Series(dtype=object)):
            if isinstance(value, (list, tuple, set)):
                reasons.extend(value)
        if override is not None and len(tickers) > 1:
            reasons.append(ReasonCode.SHARE_CLASS_DEDUPED)
        if conflict:
            reasons.append(ReasonCode.ISSUER_CLASSIFICATION_CONFLICT)
            conflicts.append(unit_id)
            raw_return = np.nan
            base["group_id"] = None
            base["group_name"] = None
            base["is_classified"] = False
        if not np.isfinite(float(raw_return)):
            reasons.append(ReasonCode.MISSING_RETURN)
            raw_return = np.nan

        display_name = override.issuer_name if override and override.issuer_name else base.get("name")
        row = {
            **base,
            # Downstream Stage-1 artifacts use security_id as the stable row
            # key even though the row may now represent multiple securities.
            # The original security IDs remain recoverable from member_tickers;
            # the override ID is the only honest stable key for a merged row.
            "security_id": base.get("security_id") if len(tickers) == 1 else unit_id,
            "issuer_id": None,
            "issuer_override_id": override.counting_unit_id if override else None,
            "counting_unit_id": unit_id,
            "counting_unit": "security_with_overrides",
            "ticker": representative,
            "representative_ticker": representative,
            "name": display_name,
            "member_tickers": list(tickers),
            "member_count": len(tickers),
            "member_weights_json": json.dumps(
                member_weights,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            "selection_method": method,
            "selection_data_through": (
                cap_session.date().isoformat()
                if method == "SHARE_CLASS_MARKET_CAP" and cap_session is not None
                else adv_session.date().isoformat()
                if adv_session is not None
                else None
            ),
            "raw_return": float(raw_return) if np.isfinite(raw_return) else np.nan,
            "raw_return_1d": float(raw_return) if np.isfinite(raw_return) else np.nan,
            "is_valid_for_headline": bool(
                not conflict and base.get("group_id") and np.isfinite(raw_return)
            ),
            "issuer_dedupe_status": (
                DedupeStatus.PARTIAL_OVERRIDES.value
                if applied_override_count > 0
                else DedupeStatus.NONE.value
            ),
            "issuer_override_version": override_set.version,
            "issuer_dedupe_source": override_set.source,
            "reason_codes": sorted_reason_codes(reasons),
        }
        output.append(row)
        if override is not None:
            override_details.append(
                {
                    "counting_unit_id": unit_id,
                    "tickers": list(tickers),
                    "representative_ticker": representative,
                    "selection_method": method,
                    "selection_data_through": row["selection_data_through"],
                    "member_weights": member_weights,
                    "adv_observation_counts": adv_counts,
                    "classification_conflict": conflict,
                }
            )

    result = pd.DataFrame(output).sort_values(
        ["group_id", "counting_unit_id"],
        na_position="last",
        kind="mergesort",
    ).reset_index(drop=True)
    dedupe_status = (
        DedupeStatus.PARTIAL_OVERRIDES
        if applied_override_count > 0
        else DedupeStatus.NONE
    )
    if not result.empty:
        result["issuer_dedupe_status"] = dedupe_status.value
    diagnostics = {
        "counting_unit": "security_with_overrides",
        "issuer_dedupe_status": dedupe_status.value,
        "issuer_overrides_applied": applied_override_count > 0,
        "issuer_override_count": applied_override_count,
        "issuer_override_version": override_set.version,
        "issuer_dedupe_source": override_set.source,
        "issuer_override_file_hash": override_set.file_hash,
        "n_security_rows": int(len(members)),
        "n_counting_units": int(len(result)),
        "classification_conflict_count": len(conflicts),
        "classification_conflict_units": sorted(conflicts),
        "override_details": override_details,
    }
    return result, diagnostics


__all__ = [
    "CLASSIFICATION_HASH_COLUMNS",
    "ClassificationValidationError",
    "GroupIdMappingValidationError",
    "IssuerOverride",
    "IssuerOverrideSet",
    "IssuerOverrideValidationError",
    "StableGroupIdMapping",
    "build_counting_units",
    "classification_hash",
    "load_issuer_overrides",
    "load_stable_group_id_mapping",
    "normalize_classification_frame",
    "normalize_ticker",
    "source_payload_hash",
]
