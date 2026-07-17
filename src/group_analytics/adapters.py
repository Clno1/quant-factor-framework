"""CLI-side adapters for current FMP classification and local EOD data.

The FMP adapter calls the FMP client directly; it never calls ``get_universe``
and therefore cannot inherit that function's Wikipedia fallback.  Its cache is
an immutable group-owned snapshot with a required provenance sidecar.  The EOD
adapter only reads the repository's shared ``data/raw/ohlcv`` Parquet files and
has no network path, which keeps Web requests artifact-only.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Callable
from uuid import uuid4

import numpy as np
import pandas as pd

from src.config import CONFIG, PROJECT_ROOT

from .classification import (
    ClassificationValidationError,
    classification_hash,
    load_stable_group_id_mapping,
    normalize_classification_frame,
    normalize_ticker,
    source_payload_hash,
)
from .models import (
    ClassificationSnapshot,
    EODMarketSnapshot,
    GroupAnalyticsError,
    ReasonCode,
    UnsupportedCombinationError,
)


_SAFE_LOCATOR = re.compile(r"^snapshots/[A-Za-z0-9._-]+$")
_CLASSIFICATION_CACHE_SCHEMA = 1


class ClassificationSourceError(GroupAnalyticsError):
    code = "CLASSIFICATION_SOURCE_UNAVAILABLE"
    stage = "classification"


class UncertifiedClassificationCacheError(GroupAnalyticsError):
    code = ReasonCode.UNKNOWN_LEGACY_CACHE.value
    stage = "classification"


class LocalMarketDataError(GroupAnalyticsError):
    code = "LOCAL_MARKET_DATA_ERROR"
    stage = "load_market_data"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _strict_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_strict_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@contextmanager
def _exclusive_file_lock(path: Path):
    """Serialize cache pointer selection across processes.

    ``flock`` is released by the kernel when the process exits, so a crashed
    writer cannot leave a permanently wedged cache lock.  The lock file holds
    no payload or credentials and is never exposed through diagnostics.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_classification_cache_root() -> Path:
    return PROJECT_ROOT / "data" / "reference" / "group_analytics" / "classifications"


class FMPCurrentClassificationProvider:
    """Current-snapshot FMP provider with a provenance-certified cache."""

    def __init__(
        self,
        *,
        cache_root: str | Path | None = None,
        cache_max_age_hours: float = 24.0,
        fetcher: Callable[[], pd.DataFrame] | None = None,
        now: Callable[[], datetime] = _utc_now,
        allow_verified_stale_cache: bool = False,
        group_id_mapping_path: str | Path | None = None,
    ) -> None:
        self.cache_root = Path(cache_root) if cache_root is not None else _default_classification_cache_root()
        self.cache_max_age_hours = max(0.0, float(cache_max_age_hours))
        self._fetcher = fetcher
        self._now = now
        self.allow_verified_stale_cache = bool(allow_verified_stale_cache)
        self.group_id_mapping = load_stable_group_id_mapping(
            group_id_mapping_path
            or PROJECT_ROOT / "configs" / "classifications" / "fmp_group_ids.yaml"
        )

    @staticmethod
    def _validate_combination(universe: str, taxonomy: str, level: str) -> tuple[str, str, str]:
        universe = str(universe).strip().upper()
        taxonomy = str(taxonomy).strip().upper()
        level = str(level).strip().lower()
        if universe != "SP500" or taxonomy != "FMP" or level not in {"sector", "sub_industry"}:
            raise UnsupportedCombinationError(
                "Stage-1 current classification supports only SP500/FMP sector or sub_industry",
                details={
                    "universe": universe,
                    "taxonomy": taxonomy,
                    "level": level,
                    "enabled": {
                        "universes": ["SP500"],
                        "taxonomies": ["FMP"],
                        "levels": ["sector", "sub_industry"],
                    },
                },
            )
        return universe, taxonomy, level

    def _combo_root(self, universe: str, taxonomy: str, level: str) -> Path:
        return self.cache_root / taxonomy / universe / level

    def _fetch(self) -> pd.DataFrame:
        if self._fetcher is not None:
            result = self._fetcher()
        else:
            # Intentional leaf import: this endpoint has no Wikipedia fallback.
            from src.data.fmp import get_sp500_constituents

            result = get_sp500_constituents()
        if not isinstance(result, pd.DataFrame) or result.empty:
            raise ClassificationSourceError("FMP returned an empty SP500 classification payload")
        return result

    def _read_pointer(self, combo_root: Path) -> tuple[Path, dict[str, Any]]:
        pointer_path = combo_root / "latest.json"
        if not pointer_path.exists():
            legacy_files = list(combo_root.glob("*.parquet")) if combo_root.exists() else []
            if legacy_files:
                raise UncertifiedClassificationCacheError(
                    "Classification cache has Parquet data without a provenance pointer/sidecar",
                    details={"legacy_file_count": len(legacy_files)},
                )
            raise FileNotFoundError("classification cache pointer is absent")
        try:
            pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise UncertifiedClassificationCacheError(
                "Classification cache pointer is unreadable",
                details={"pointer_locator": "latest.json"},
            ) from exc
        if not isinstance(pointer, dict):
            raise UncertifiedClassificationCacheError(
                "Classification cache pointer must be a JSON object",
                details={"pointer_locator": "latest.json"},
            )
        locator = str(pointer.get("locator") or "")
        if not _SAFE_LOCATOR.fullmatch(locator):
            raise UncertifiedClassificationCacheError(
                "Classification cache pointer has an unsafe locator",
                details={"locator_valid": False},
            )
        snapshot_dir = combo_root / locator
        try:
            snapshot_dir.resolve().relative_to(combo_root.resolve())
        except ValueError as exc:
            raise UncertifiedClassificationCacheError(
                "Classification cache locator escapes its root"
            ) from exc
        return snapshot_dir, pointer

    def _read_certified_snapshot(
        self,
        *,
        universe: str,
        taxonomy: str,
        level: str,
        combo_root: Path,
        snapshot_dir: Path,
        pointer: dict[str, Any] | None,
        allow_stale: bool,
        validate_clock: bool = True,
    ) -> ClassificationSnapshot:
        try:
            locator = snapshot_dir.relative_to(combo_root).as_posix()
            snapshot_dir.resolve().relative_to(combo_root.resolve())
        except ValueError as exc:
            raise UncertifiedClassificationCacheError(
                "Classification cache snapshot escapes its root"
            ) from exc
        if not _SAFE_LOCATOR.fullmatch(locator) or snapshot_dir.is_symlink():
            raise UncertifiedClassificationCacheError(
                "Classification cache snapshot locator is unsafe",
                details={"snapshot_locator": locator},
            )

        data_path = snapshot_dir / "classification.parquet"
        provenance_path = snapshot_dir / "provenance.json"
        if not data_path.is_file() or not provenance_path.is_file():
            raise UncertifiedClassificationCacheError(
                "Classification cache snapshot is missing data or provenance sidecar",
                details={"snapshot_locator": locator},
            )
        try:
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise UncertifiedClassificationCacheError(
                "Classification provenance sidecar is unreadable",
                details={"sidecar_locator": f"{locator}/provenance.json"},
            ) from exc
        if not isinstance(provenance, dict):
            raise UncertifiedClassificationCacheError(
                "Classification provenance sidecar must be a JSON object",
                details={"sidecar_locator": f"{locator}/provenance.json"},
            )
        expected = {
            "schema_version": _CLASSIFICATION_CACHE_SCHEMA,
            "provider": "FMP",
            "taxonomy": taxonomy,
            "universe": universe,
            "level": level,
        }
        mismatches = {
            key: {"expected": value, "actual": provenance.get(key)}
            for key, value in expected.items()
            if provenance.get(key) != value
        }
        if provenance.get("group_id_mapping_version") != self.group_id_mapping.version:
            mismatches["group_id_mapping_version"] = {
                "expected": self.group_id_mapping.version,
                "actual": provenance.get("group_id_mapping_version"),
            }
        if mismatches:
            raise UncertifiedClassificationCacheError(
                "Classification provenance does not match the requested combination",
                details={"mismatch_fields": sorted(mismatches)},
            )
        if pointer is not None:
            if pointer.get("classification_hash") != provenance.get("classification_hash"):
                raise UncertifiedClassificationCacheError(
                    "Classification cache pointer and sidecar hashes disagree"
                )
            if pointer.get("updated_at") != provenance.get("fetched_at"):
                raise UncertifiedClassificationCacheError(
                    "Classification cache pointer and sidecar timestamps disagree"
                )
        try:
            actual_file_hash = _file_hash(data_path)
        except OSError:
            raise UncertifiedClassificationCacheError(
                "Classification Parquet cannot be hashed"
            ) from None
        if actual_file_hash != provenance.get("file_hash"):
            raise UncertifiedClassificationCacheError(
                "Classification Parquet hash does not match its sidecar"
            )
        try:
            frame = pd.read_parquet(data_path)
        except Exception as exc:  # noqa: BLE001
            raise UncertifiedClassificationCacheError(
                "Classification Parquet cannot be read",
                details={"data_locator": f"{locator}/classification.parquet"},
            ) from exc
        calculated = classification_hash(frame)
        if calculated != provenance.get("classification_hash"):
            raise UncertifiedClassificationCacheError(
                "Classification semantic hash does not match its sidecar"
            )
        try:
            row_count = int(provenance.get("row_count", -1))
        except (TypeError, ValueError):
            row_count = -1
        if row_count != len(frame):
            raise UncertifiedClassificationCacheError(
                "Classification row count does not match its sidecar"
            )
        fetched_at = str(provenance.get("fetched_at") or "")
        try:
            fetched_datetime = _parse_utc(fetched_at)
        except (TypeError, ValueError) as exc:
            raise UncertifiedClassificationCacheError(
                "Classification provenance has an invalid fetched_at"
            ) from exc
        current = self._now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        age_hours = (current - fetched_datetime).total_seconds() / 3600.0
        if validate_clock and age_hours < -1 / 60:
            raise UncertifiedClassificationCacheError(
                "Classification cache fetched_at is in the future"
            )
        if age_hours > self.cache_max_age_hours and not allow_stale:
            raise TimeoutError(f"Certified classification cache is {age_hours:.2f} hours old")
        required_text = {
            key: str(provenance.get(key) or "").strip()
            for key in (
                "taxonomy_version",
                "classification_hash",
                "classification_asof",
                "group_id_mapping_version",
            )
        }
        if not all(required_text.values()):
            raise UncertifiedClassificationCacheError(
                "Classification provenance is missing required identity fields"
            )
        return ClassificationSnapshot(
            frame=frame,
            provider="FMP",
            taxonomy_version=required_text["taxonomy_version"],
            classification_hash=calculated,
            classification_asof=required_text["classification_asof"],
            fetched_at=fetched_at,
            group_id_mapping_version=required_text["group_id_mapping_version"],
            fallback=age_hours > self.cache_max_age_hours,
            payload_hash=str(provenance.get("payload_hash") or "") or None,
            source_path=data_path,
            diagnostics=[
                {
                    "code": "CERTIFIED_CLASSIFICATION_CACHE",
                    "age_hours": age_hours,
                    "stale": age_hours > self.cache_max_age_hours,
                    "provenance_locator": f"{locator}/provenance.json",
                }
            ],
        )

    def _read_certified_cache(
        self,
        *,
        universe: str,
        taxonomy: str,
        level: str,
        allow_stale: bool,
    ) -> ClassificationSnapshot:
        combo_root = self._combo_root(universe, taxonomy, level)
        snapshot_dir, pointer = self._read_pointer(combo_root)
        return self._read_certified_snapshot(
            universe=universe,
            taxonomy=taxonomy,
            level=level,
            combo_root=combo_root,
            snapshot_dir=snapshot_dir,
            pointer=pointer,
            allow_stale=allow_stale,
        )

    def _newest_certified_snapshot(
        self,
        *,
        universe: str,
        taxonomy: str,
        level: str,
        validate_clock: bool,
    ) -> ClassificationSnapshot:
        combo_root = self._combo_root(universe, taxonomy, level)
        snapshots_root = combo_root / "snapshots"
        candidates: list[ClassificationSnapshot] = []
        if snapshots_root.is_dir() and not snapshots_root.is_symlink():
            for snapshot_dir in sorted(snapshots_root.iterdir()):
                if not snapshot_dir.is_dir() or snapshot_dir.is_symlink():
                    continue
                try:
                    candidate = self._read_certified_snapshot(
                        universe=universe,
                        taxonomy=taxonomy,
                        level=level,
                        combo_root=combo_root,
                        snapshot_dir=snapshot_dir,
                        pointer=None,
                        allow_stale=True,
                        validate_clock=validate_clock,
                    )
                except (OSError, TimeoutError, UncertifiedClassificationCacheError):
                    continue
                candidates.append(candidate)
        if not candidates:
            raise UncertifiedClassificationCacheError(
                "No certified classification snapshot is available for recovery"
            )
        return max(
            candidates,
            key=lambda value: (
                _parse_utc(value.fetched_at),
                value.classification_hash,
                value.source_path.parent.name if value.source_path is not None else "",
            ),
        )

    @staticmethod
    def _pointer_for(snapshot: ClassificationSnapshot, combo_root: Path) -> dict[str, Any]:
        if snapshot.source_path is None:
            raise UncertifiedClassificationCacheError(
                "Certified classification snapshot has no cache locator"
            )
        locator = snapshot.source_path.parent.relative_to(combo_root).as_posix()
        if not _SAFE_LOCATOR.fullmatch(locator):
            raise UncertifiedClassificationCacheError(
                "Certified classification snapshot has an unsafe cache locator"
            )
        return {
            "schema_version": _CLASSIFICATION_CACHE_SCHEMA,
            "locator": locator,
            "classification_hash": snapshot.classification_hash,
            "taxonomy_version": snapshot.taxonomy_version,
            "updated_at": snapshot.fetched_at,
        }

    def _recover_certified_cache(
        self,
        *,
        universe: str,
        taxonomy: str,
        level: str,
    ) -> ClassificationSnapshot:
        combo_root = self._combo_root(universe, taxonomy, level)
        with _exclusive_file_lock(combo_root / ".publish.lock"):
            recovered = self._newest_certified_snapshot(
                universe=universe,
                taxonomy=taxonomy,
                level=level,
                validate_clock=True,
            )
            _atomic_json(combo_root / "latest.json", self._pointer_for(recovered, combo_root))
        recovered.diagnostics.append(
            {"code": "CLASSIFICATION_CACHE_POINTER_RECOVERED"}
        )
        return recovered

    def _publish_cache(
        self,
        *,
        universe: str,
        taxonomy: str,
        level: str,
        snapshot: ClassificationSnapshot,
    ) -> ClassificationSnapshot:
        combo_root = self._combo_root(universe, taxonomy, level)
        snapshots_root = combo_root / "snapshots"
        snapshots_root.mkdir(parents=True, exist_ok=True)
        try:
            fetched_datetime = _parse_utc(snapshot.fetched_at)
        except (TypeError, ValueError) as exc:
            raise UncertifiedClassificationCacheError(
                "Classification snapshot has an invalid fetched_at"
            ) from exc
        if snapshot.group_id_mapping_version != self.group_id_mapping.version:
            raise UncertifiedClassificationCacheError(
                "Classification snapshot uses an unrecognized group-id mapping version"
            )
        timestamp = fetched_datetime.strftime("%Y%m%dT%H%M%S%fZ")
        snapshot_id = f"{timestamp}_{snapshot.classification_hash[:16]}_{uuid4().hex[:8]}"
        staging = combo_root / f".tmp_{snapshot_id}"
        destination = snapshots_root / snapshot_id
        staging.mkdir(parents=True, exist_ok=False)
        data_path = staging / "classification.parquet"
        try:
            snapshot.frame.to_parquet(data_path, compression="snappy", index=False)
            provenance = {
                "schema_version": _CLASSIFICATION_CACHE_SCHEMA,
                "provider": "FMP",
                "source_endpoint": "/sp500-constituent",
                "taxonomy": taxonomy,
                "universe": universe,
                "level": level,
                "fetched_at": snapshot.fetched_at,
                "classification_asof": snapshot.classification_asof,
                "taxonomy_version": snapshot.taxonomy_version,
                "classification_hash": snapshot.classification_hash,
                "group_id_mapping_version": snapshot.group_id_mapping_version,
                "payload_hash": snapshot.payload_hash,
                "row_count": int(len(snapshot.frame)),
                "columns": list(snapshot.frame.columns),
                "file_hash": _file_hash(data_path),
            }
            _atomic_json(staging / "provenance.json", provenance)
            os.replace(staging, destination)
            published = self._read_certified_snapshot(
                universe=universe,
                taxonomy=taxonomy,
                level=level,
                combo_root=combo_root,
                snapshot_dir=destination,
                pointer=None,
                allow_stale=True,
                validate_clock=False,
            )
            with _exclusive_file_lock(combo_root / ".publish.lock"):
                # The scan is intentionally inside the lock.  Every candidate
                # is immutable and provenance-verified; selecting the maximum
                # fetched_at prevents a slow, older fetch from rolling latest
                # backward after a newer writer has already completed.
                selected = self._newest_certified_snapshot(
                    universe=universe,
                    taxonomy=taxonomy,
                    level=level,
                    validate_clock=False,
                )
                _atomic_json(
                    combo_root / "latest.json",
                    self._pointer_for(selected, combo_root),
                )
        except Exception:
            if staging.exists():
                for child in staging.iterdir():
                    child.unlink(missing_ok=True)
                staging.rmdir()
            raise
        if selected.source_path == published.source_path:
            selected.diagnostics = [
                *snapshot.diagnostics,
                {
                    "code": "FMP_CLASSIFICATION_FETCH",
                    "cache_locator": f"snapshots/{snapshot_id}",
                    "provenance_locator": f"snapshots/{snapshot_id}/provenance.json",
                },
            ]
        else:
            selected.diagnostics.append(
                {
                    "code": "CLASSIFICATION_CACHE_NEWER_SNAPSHOT_RETAINED",
                    "candidate_fetched_at": snapshot.fetched_at,
                    "retained_fetched_at": selected.fetched_at,
                }
            )
        return selected

    def snapshot(
        self,
        *,
        universe: str,
        taxonomy: str,
        level: str,
        asof: str,
        force: bool = False,
    ) -> ClassificationSnapshot:
        universe, taxonomy, level = self._validate_combination(universe, taxonomy, level)
        cache_error: Exception | None = None
        if not force:
            try:
                return self._read_certified_cache(
                    universe=universe,
                    taxonomy=taxonomy,
                    level=level,
                    allow_stale=False,
                )
            except (FileNotFoundError, OSError, TimeoutError, UncertifiedClassificationCacheError) as exc:
                cache_error = exc

        try:
            payload = self._fetch()
            fetched_at = _iso_utc(self._now())
            classification_asof = fetched_at[:10]
            normalized = normalize_classification_frame(
                payload,
                taxonomy=taxonomy,
                level=level,
                classification_asof=classification_asof,
                fetched_at=fetched_at,
                source="FMP",
                group_id_mapping=self.group_id_mapping,
            )
            semantic_hash = classification_hash(normalized)
            taxonomy_version = (
                f"fmp-{universe.casefold()}-{classification_asof}-"
                f"map-{self.group_id_mapping.version}-{semantic_hash[:12]}"
            )
            normalized["taxonomy_version"] = taxonomy_version
            result = ClassificationSnapshot(
                frame=normalized,
                provider="FMP",
                taxonomy_version=taxonomy_version,
                classification_hash=semantic_hash,
                classification_asof=classification_asof,
                fetched_at=fetched_at,
                group_id_mapping_version=self.group_id_mapping.version,
                fallback=False,
                payload_hash=source_payload_hash(payload),
                diagnostics=[],
            )
            return self._publish_cache(
                universe=universe,
                taxonomy=taxonomy,
                level=level,
                snapshot=result,
            )
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (ClassificationValidationError, UnsupportedCombinationError)):
                raise
            if self.allow_verified_stale_cache:
                stale: ClassificationSnapshot | None = None
                try:
                    stale = self._read_certified_cache(
                        universe=universe,
                        taxonomy=taxonomy,
                        level=level,
                        allow_stale=True,
                    )
                except Exception:
                    try:
                        stale = self._recover_certified_cache(
                            universe=universe,
                            taxonomy=taxonomy,
                            level=level,
                        )
                    except Exception:
                        stale = None
                if stale is not None:
                    stale.diagnostics.append(
                        {
                            "code": "FMP_FETCH_FAILED_VERIFIED_STALE_CACHE_USED",
                            "error_type": type(exc).__name__,
                        }
                    )
                    return stale
            details: dict[str, Any] = {
                "provider": "FMP",
                "universe": universe,
                "taxonomy": taxonomy,
                "level": level,
                "fetch_error_type": type(exc).__name__,
            }
            if cache_error is not None:
                details["cache_error_type"] = type(cache_error).__name__
                if isinstance(cache_error, UncertifiedClassificationCacheError):
                    details["reason_codes"] = [ReasonCode.UNKNOWN_LEGACY_CACHE.value]
            raise ClassificationSourceError(
                "Unable to obtain a certified current FMP classification snapshot",
                details=details,
            ) from None


def _default_raw_ohlcv_root() -> Path:
    configured = Path(str(CONFIG.data.raw_dir))
    root = configured if configured.is_absolute() else PROJECT_ROOT / configured
    return root / "ohlcv"


def _default_market_cap_path() -> Path:
    configured = Path(str(CONFIG.data.processed_dir))
    root = configured if configured.is_absolute() else PROJECT_ROOT / configured
    return root / "SP500" / "market_cap.parquet"


class LocalEODMarketDataProvider:
    """Read shared local OHLCV caches without downloading or mutating them."""

    def __init__(
        self,
        *,
        raw_ohlcv_root: str | Path | None = None,
        market_cap_path: str | Path | None = None,
    ) -> None:
        self.raw_ohlcv_root = Path(raw_ohlcv_root) if raw_ohlcv_root is not None else _default_raw_ohlcv_root()
        self.market_cap_path = (
            Path(market_cap_path)
            if market_cap_path is not None
            else _default_market_cap_path()
        )
        self.last_diagnostics: dict[str, Any] = {}

    def _path(self, symbol: str) -> Path:
        ticker = normalize_ticker(symbol)
        path = self.raw_ohlcv_root / f"{ticker}.parquet"
        try:
            path.resolve().relative_to(self.raw_ohlcv_root.resolve())
        except ValueError as exc:
            raise LocalMarketDataError(f"Unsafe local ticker path: {symbol!r}") from exc
        return path

    @staticmethod
    def _read_one(path: Path, ticker: str) -> pd.DataFrame:
        try:
            frame = pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001
            raise LocalMarketDataError(
                f"Unable to read local OHLCV cache for {ticker}",
                details={"path": str(path)},
            ) from exc
        if "date" in frame.columns and isinstance(frame.index, pd.RangeIndex):
            frame = frame.set_index("date")
        if "adj_close" not in frame.columns:
            raise LocalMarketDataError(
                f"Local OHLCV cache for {ticker} has no adj_close column",
                details={"path": str(path), "columns": list(frame.columns)},
            )
        index = pd.to_datetime(frame.index, errors="coerce")
        if index.isna().any():
            raise LocalMarketDataError(f"Local OHLCV cache for {ticker} has invalid dates")
        if index.tz is not None:
            index = index.tz_convert("America/New_York").tz_localize(None)
        index = index.normalize()
        if index.has_duplicates:
            raise LocalMarketDataError(f"Local OHLCV cache for {ticker} has duplicate sessions")
        out = pd.DataFrame(index=index)
        out["adj_close"] = pd.to_numeric(frame["adj_close"].to_numpy(), errors="coerce")
        out["volume"] = (
            pd.to_numeric(frame["volume"].to_numpy(), errors="coerce")
            if "volume" in frame.columns
            else np.nan
        )
        out.index.name = "date"
        return out.sort_index()

    def _read_market_cap(self, symbols: list[str]) -> tuple[pd.DataFrame | None, Path | None]:
        if self.market_cap_path is None or not self.market_cap_path.exists():
            return None, None
        try:
            frame = pd.read_parquet(self.market_cap_path)
        except Exception as exc:  # noqa: BLE001
            raise LocalMarketDataError(
                "Unable to read optional market-cap matrix",
                details={"path": str(self.market_cap_path)},
            ) from exc
        frame.columns = [normalize_ticker(column) for column in frame.columns]
        if frame.columns.has_duplicates:
            raise LocalMarketDataError("market-cap matrix has duplicate ticker columns")
        index = pd.to_datetime(frame.index, errors="coerce")
        if index.isna().any() or index.has_duplicates:
            raise LocalMarketDataError("market-cap matrix has invalid or duplicate sessions")
        if index.tz is not None:
            index = index.tz_convert("America/New_York").tz_localize(None)
        frame.index = index.normalize()
        frame = frame.apply(pd.to_numeric, errors="coerce").sort_index().reindex(columns=symbols)
        return frame, self.market_cap_path

    def snapshot(
        self,
        *,
        symbols: list[str],
        benchmark: str,
        force: bool = False,
    ) -> EODMarketSnapshot:
        # ``force`` deliberately does not download: this adapter is local-only.
        del force
        normalized = list(dict.fromkeys(normalize_ticker(symbol) for symbol in symbols))
        benchmark = normalize_ticker(benchmark)
        frames: dict[str, pd.DataFrame] = {}
        input_paths: list[Path] = []
        missing: list[str] = []
        for ticker in [*normalized, benchmark]:
            if ticker in frames:
                continue
            path = self._path(ticker)
            if not path.exists():
                missing.append(ticker)
                continue
            frames[ticker] = self._read_one(path, ticker)
            input_paths.append(path)

        all_dates = pd.DatetimeIndex([])
        for frame in frames.values():
            all_dates = all_dates.union(pd.DatetimeIndex(frame.index))
        all_dates = all_dates.sort_values()
        adj_close = pd.DataFrame(index=all_dates, columns=normalized, dtype=float)
        volume = pd.DataFrame(index=all_dates, columns=normalized, dtype=float)
        for ticker in normalized:
            frame = frames.get(ticker)
            if frame is not None:
                adj_close[ticker] = frame["adj_close"].reindex(all_dates)
                volume[ticker] = frame["volume"].reindex(all_dates)
        benchmark_frame = pd.DataFrame(index=all_dates, columns=[benchmark], dtype=float)
        if benchmark in frames:
            benchmark_frame[benchmark] = frames[benchmark]["adj_close"].reindex(all_dates)
        market_cap, market_cap_path = self._read_market_cap(normalized)
        if market_cap_path is not None:
            input_paths.append(market_cap_path)
        self.last_diagnostics = {
            "provider": "LOCAL_SHARED_RAW_OHLCV",
            "network_access": False,
            "raw_ohlcv_root": str(self.raw_ohlcv_root),
            "symbols_requested": len(normalized),
            "symbols_loaded": sum(ticker in frames for ticker in normalized),
            "missing_symbols": sorted(set(missing)),
            "benchmark": benchmark,
            "benchmark_loaded": benchmark in frames,
            "source_max_date": (
                all_dates.max().date().isoformat() if len(all_dates) else None
            ),
        }
        return EODMarketSnapshot(
            adj_close=adj_close,
            volume=volume,
            benchmark_adj_close=benchmark_frame,
            market_cap=market_cap,
            input_paths=input_paths,
        )


__all__ = [
    "ClassificationSourceError",
    "FMPCurrentClassificationProvider",
    "LocalEODMarketDataProvider",
    "LocalMarketDataError",
    "UncertifiedClassificationCacheError",
]
