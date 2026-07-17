"""Completed-session momentum source for the premarket digest."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src.alerts.config import AlertSettings
from src.breakouts import BreakoutFilters, evaluate_daily_setup, load_market_regime
from src.breakouts.scanner import load_daily_frame
from src.config import PROJECT_ROOT

from .models import SourceGateError
from .settings import PremarketDigestSettings


_STATUS_ORDER = {"BREAKOUT": 0, "READY": 1, "SETUP": 2, "FORMING": 3}
_UNIVERSE_MANIFEST_SCHEMA = 1


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_cached_universe(name: str) -> pd.DataFrame:
    cache_names = {
        "US_ACTIVE": "us_active.parquet",
        "SP500": "sp500.parquet",
    }
    normalized = str(name).strip().upper()
    filename = cache_names.get(normalized)
    if filename is None:
        raise SourceGateError(
            "MOMENTUM_UNIVERSE_CACHE_UNSUPPORTED",
            "premarket momentum only accepts a refresh-owned cached universe",
            details={"universe": normalized},
        )
    path = Path(PROJECT_ROOT) / "data" / "raw" / "universe" / filename
    if not path.is_file():
        raise SourceGateError(
            "MOMENTUM_UNIVERSE_CACHE_MISSING",
            "the completed-session universe cache is missing",
            details={"universe": normalized},
        )
    manifest_path = path.with_suffix(".premarket.json")
    if not manifest_path.is_file():
        raise SourceGateError(
            "MOMENTUM_UNIVERSE_MANIFEST_MISSING",
            "the refresh-owned universe manifest is missing",
            details={"universe": normalized},
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        if int(manifest.get("schema_version")) != _UNIVERSE_MANIFEST_SCHEMA:
            raise ValueError("unsupported manifest schema")
        if str(manifest.get("universe") or "").upper() != normalized:
            raise ValueError("manifest universe mismatch")
        refreshed_at = pd.Timestamp(manifest.get("refreshed_at"))
        if refreshed_at.tzinfo is None:
            raise ValueError("refreshed_at must include a timezone")
        now_utc = pd.Timestamp.now(tz="UTC")
        if refreshed_at.tz_convert("UTC") > now_utc + pd.Timedelta(minutes=5):
            raise ValueError("refreshed_at is in the future")
        initial_stat = path.stat()
        actual_hash = _sha256_path(path)
        if str(manifest.get("parquet_sha256") or "") != actual_hash:
            raise ValueError("parquet hash mismatch")
        frame = pd.read_parquet(path)
        final_stat = path.stat()
        if (
            initial_stat.st_mtime_ns,
            initial_stat.st_size,
            initial_stat.st_ino,
        ) != (
            final_stat.st_mtime_ns,
            final_stat.st_size,
            final_stat.st_ino,
        ):
            raise ValueError("universe cache changed while it was being read")
        if int(manifest.get("row_count")) != len(frame):
            raise ValueError("manifest row count mismatch")
        frame.attrs.update(
            {
                "cache_mtime_utc": datetime.fromtimestamp(
                    final_stat.st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
                "manifest_source_session": str(manifest.get("source_session") or ""),
                "manifest_refreshed_at": refreshed_at.isoformat(),
                "manifest_sha256": actual_hash,
            }
        )
        return frame
    except SourceGateError:
        raise
    except Exception as exc:
        raise SourceGateError(
            "MOMENTUM_UNIVERSE_CACHE_INVALID",
            "the completed-session universe cache or manifest is invalid",
            details={"universe": normalized, "error_type": type(exc).__name__},
        ) from None


def _text(value: Any) -> str:
    if value is None or value is pd.NA:
        return ""
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fingerprint(source_session: str, rows: list[dict[str, Any]]) -> str:
    material = [
        {
            "ticker": _text(row.get("ticker")),
            "data_date": _text(row.get("data_date")),
            "close": _finite(row.get("close")),
            "status": _text(row.get("status")),
            "score": _finite(row.get("score")),
        }
        for row in rows
    ]
    encoded = json.dumps(
        {"source_session": source_session, "rows": material},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class CompletedSessionMomentumSource:
    """Evaluate the existing breakout algorithm using only the final T-1 bar."""

    def __init__(
        self,
        settings: PremarketDigestSettings,
        *,
        alert_settings: AlertSettings | None = None,
        universe_loader: Callable[[str], pd.DataFrame] = _load_cached_universe,
        frame_loader: Callable[[str], pd.DataFrame] = load_daily_frame,
        regime_loader: Callable[..., dict[str, Any]] = load_market_regime,
    ) -> None:
        self.settings = settings
        # The CLI has already loaded its explicitly selected environment file.
        # Do not let this leaf adapter silently merge .env.local afterwards.
        self.alert_settings = alert_settings or AlertSettings.load(
            load_env=False,
            include_environment_tickers=False,
        )
        self.universe_loader = universe_loader
        self.frame_loader = frame_loader
        self.regime_loader = regime_loader

    def load(self, source_session: str) -> dict[str, Any]:
        loaded_universe = self.universe_loader(self.settings.momentum_universe)
        universe_cache_mtime_utc = loaded_universe.attrs.get("cache_mtime_utc")
        universe_manifest_source_session = loaded_universe.attrs.get(
            "manifest_source_session"
        )
        universe_manifest_refreshed_at = loaded_universe.attrs.get(
            "manifest_refreshed_at"
        )
        target = pd.Timestamp(source_session).normalize()
        if (
            universe_manifest_source_session is not None
            and str(universe_manifest_source_session) != source_session
        ):
            raise SourceGateError(
                "MOMENTUM_UNIVERSE_CACHE_STALE",
                "the universe cache was not refreshed for the completed source session",
                details={
                    "universe": self.settings.momentum_universe,
                    "source_session": source_session,
                    "manifest_source_session": str(universe_manifest_source_session),
                    "manifest_refreshed_at": str(universe_manifest_refreshed_at or ""),
                },
            )
        universe = loaded_universe.copy()
        required = {"ticker", "asset_type", "current_dollar_volume"}
        missing = sorted(required.difference(universe.columns))
        if missing:
            raise SourceGateError(
                "MOMENTUM_UNIVERSE_SCHEMA",
                "momentum universe lacks required stock/liquidity fields",
                details={"missing_columns": missing},
            )
        universe = universe.loc[universe["ticker"].notna()].copy()
        universe["ticker"] = universe["ticker"].astype(str).str.strip().str.upper()
        universe = universe.loc[universe["ticker"].ne("")].copy()
        asset_type = universe["asset_type"].fillna("").astype(str).str.upper()
        if not self.settings.momentum_include_etfs:
            universe = universe.loc[asset_type.eq("STOCK")].copy()
        else:
            universe = universe.loc[asset_type.isin({"STOCK", "ETF"})].copy()
        liquidity = pd.to_numeric(universe["current_dollar_volume"], errors="coerce")
        always = {
            str(ticker).strip().upper()
            for ticker in self.alert_settings.always_tickers
            if str(ticker).strip()
        }
        universe = universe.loc[
            liquidity.ge(self.alert_settings.broad_min_current_dollar_volume)
            | universe["ticker"].isin(always)
        ].copy()
        universe = universe.drop_duplicates("ticker", keep="first")
        if universe.empty:
            raise SourceGateError(
                "MOMENTUM_EMPTY_UNIVERSE",
                "no securities remain after the stock/liquidity gate",
            )

        names = universe.set_index("ticker").get("name", pd.Series(dtype="object"))
        sectors = universe.set_index("ticker").get("sector", pd.Series(dtype="object"))
        filters = BreakoutFilters(
            min_return_20d=self.alert_settings.strict_min_return_20d,
            min_adr_20d=self.alert_settings.strict_min_adr_20d,
            min_dollar_volume=self.alert_settings.strict_min_dollar_volume,
            min_avg_dollar_volume=self.alert_settings.strict_min_avg_dollar_volume,
            max_results=1000,
        )
        loaded_count = 0
        exact_count = 0
        evaluable_count = 0
        rows: list[dict[str, Any]] = []
        missing_tickers: list[str] = []
        stale_tickers: list[str] = []
        insufficient_history_tickers: list[str] = []
        for ticker in universe["ticker"].tolist():
            frame = self.frame_loader(ticker)
            if frame is None or frame.empty:
                missing_tickers.append(ticker)
                continue
            loaded_count += 1
            eligible_history = frame.copy()
            normalized_index = pd.to_datetime(eligible_history.index, errors="coerce")
            if normalized_index.tz is not None:
                normalized_index = normalized_index.tz_localize(None)
            eligible_history.index = normalized_index
            eligible_history = eligible_history.loc[
                ~eligible_history.index.isna() & (eligible_history.index <= target)
            ]
            if eligible_history.empty:
                stale_tickers.append(ticker)
                continue
            latest = pd.Timestamp(eligible_history.index.max()).normalize()
            if latest != target:
                stale_tickers.append(ticker)
                continue
            exact_count += 1
            session_labels = pd.DatetimeIndex(eligible_history.index).normalize()
            if session_labels.duplicated().any():
                insufficient_history_tickers.append(ticker)
                continue
            metric = evaluate_daily_setup(
                eligible_history,
                ticker=ticker,
                filters=filters,
                asof=target,
                name=_text(names.get(ticker)),
                sector=_text(sectors.get(ticker)),
            )
            required_metrics = (
                "close",
                "return_20d",
                "adr_20d",
                "dollar_volume",
                "avg_dollar_volume_20d",
                "pivot",
                "score",
            )
            if (
                metric is None
                or _text(metric.get("data_date")) != source_session
                or any(_finite(metric.get(key)) is None for key in required_metrics)
            ):
                insufficient_history_tickers.append(ticker)
                continue
            evaluable_count += 1
            if metric is not None and metric.get("base_pass"):
                rows.append(metric)

        universe_count = len(universe)
        exact_coverage = exact_count / universe_count
        if exact_coverage < self.settings.momentum_min_exact_asof_coverage:
            raise SourceGateError(
                "MOMENTUM_LOW_EXACT_ASOF_COVERAGE",
                "completed-session momentum cache coverage is below the send gate",
                details={
                    "source_session": source_session,
                    "universe_count": universe_count,
                    "exact_asof_count": exact_count,
                    "exact_asof_coverage": round(exact_coverage, 6),
                    "minimum": self.settings.momentum_min_exact_asof_coverage,
                },
            )
        evaluable_coverage = evaluable_count / universe_count
        if evaluable_coverage < self.settings.momentum_min_evaluable_coverage:
            raise SourceGateError(
                "MOMENTUM_LOW_EVALUABLE_COVERAGE",
                "momentum history coverage is below the completed-session send gate",
                details={
                    "source_session": source_session,
                    "universe_count": universe_count,
                    "evaluable_history_count": evaluable_count,
                    "evaluable_history_coverage": round(evaluable_coverage, 6),
                    "minimum": self.settings.momentum_min_evaluable_coverage,
                    "insufficient_history_count": len(insufficient_history_tickers),
                    "insufficient_history_sample": insufficient_history_tickers[:20],
                },
            )

        rows = [
            row
            for row in rows
            if _text(row.get("data_date")) == source_session
            and _finite(row.get("return_20d")) is not None
        ]
        if rows:
            returns = pd.Series(
                {_text(row["ticker"]): _finite(row["return_20d"]) for row in rows}
            ).rank(pct=True)
            for row in rows:
                percentile = float(returns.loc[_text(row["ticker"])] * 100.0)
                row["relative_strength_pct"] = percentile
            rows.sort(
                key=lambda row: (
                    _STATUS_ORDER.get(_text(row.get("status")), 9),
                    -int(_finite(row.get("score")) or 0),
                    -float(_finite(row.get("return_20d")) or 0.0),
                    _text(row.get("ticker")),
                )
            )

        try:
            regime = self.regime_loader(
                asof=source_session,
                symbol="QQQ",
                fetch_missing=False,
            )
        except Exception:  # source failure is informational for this field
            regime = {
                "symbol": "QQQ",
                "status": "UNKNOWN",
                "passed": None,
                "asof": source_session,
                "reason": "market-regime cache could not be read",
            }
        if str(regime.get("asof") or "") != source_session:
            regime = {
                "symbol": "QQQ",
                "status": "UNKNOWN",
                "passed": None,
                "asof": str(regime.get("asof") or "n/a"),
                "reason": "market-regime cache is not from the source session",
            }

        clean_rows = rows
        return {
            "source_session": source_session,
            "universe": self.settings.momentum_universe,
            "asset_scope": (
                "stocks_and_etfs" if self.settings.momentum_include_etfs else "stocks"
            ),
            "universe_count": universe_count,
            "loaded_count": loaded_count,
            "exact_asof_count": exact_count,
            "exact_asof_coverage": exact_coverage,
            "evaluable_history_count": evaluable_count,
            "evaluable_history_coverage": evaluable_coverage,
            "insufficient_history_count": len(insufficient_history_tickers),
            "universe_cache_mtime_utc": universe_cache_mtime_utc,
            "universe_manifest_source_session": universe_manifest_source_session,
            "universe_manifest_refreshed_at": universe_manifest_refreshed_at,
            "candidate_count": len(clean_rows),
            "breakout_count": sum(row.get("status") == "BREAKOUT" for row in clean_rows),
            "ready_count": sum(row.get("status") == "READY" for row in clean_rows),
            "setup_count": sum(row.get("status") == "SETUP" for row in clean_rows),
            "forming_count": sum(row.get("status") == "FORMING" for row in clean_rows),
            "missing_count": len(missing_tickers),
            "stale_count": len(stale_tickers),
            "market_regime": regime,
            "input_fingerprint": _fingerprint(source_session, clean_rows),
            "rows": clean_rows,
        }


__all__ = ["CompletedSessionMomentumSource"]
