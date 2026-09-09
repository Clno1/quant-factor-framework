"""Validated settings for the market turning-point research pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from src.config import CONFIG
from src.utils.date_utils import parse_date_str
from src.utils.identifiers import canonical_ticker, safe_path_component


@dataclass(frozen=True, slots=True)
class PriceInstrumentSettings:
    """One market series and the first date expected from its provider."""

    symbol: str
    start: str
    kind: str = "etf"


def _default_instruments() -> tuple[PriceInstrumentSettings, ...]:
    return (
        PriceInstrumentSettings("^GSPC", "1990-01-01", "index"),
        PriceInstrumentSettings("^NDX", "1990-01-01", "index"),
        PriceInstrumentSettings("SPY", "1993-01-29", "etf"),
        PriceInstrumentSettings("QQQ", "1999-03-10", "etf"),
        PriceInstrumentSettings("IWM", "2000-05-26", "etf"),
        PriceInstrumentSettings("HYG", "2007-04-11", "etf"),
        PriceInstrumentSettings("LQD", "2002-07-30", "etf"),
    )


@dataclass(frozen=True, slots=True)
class LabelSettings:
    """Causal preconditions and future first-touch outcome definitions."""

    horizons: tuple[int, ...] = (5, 20, 60)
    volatility_window: int = 20
    high_lookback: int = 252
    top_near_high_pct: float = 0.03
    bottom_min_drawdown_pct: float = 0.10
    bottom_drawdown_quantile: float = 0.20
    bottom_quantile_lookback: int = 1260
    minimum_history: int = 252
    barrier_vol_multiplier: float = 1.0
    minimum_barrier_pct: float = 0.02


@dataclass(frozen=True, slots=True)
class FeatureSettings:
    """Lookbacks and minimum cross-sectional coverage for P0 features."""

    moving_average_windows: tuple[int, ...] = (20, 50, 60, 120, 200)
    breadth_change_windows: tuple[int, ...] = (5, 20)
    realized_volatility_windows: tuple[int, ...] = (20, 60)
    correlation_window: int = 60
    correlation_min_members: int = 30
    momentum_lookback: int = 252
    momentum_skip: int = 21
    momentum_quantile: float = 0.10
    min_cross_section_members: int = 30
    min_cross_section_coverage: float = 0.95


@dataclass(frozen=True, slots=True)
class PITSettings:
    """Rules for publishing reconstructed historical universe membership."""

    universe: str = "SP500"
    data_universe: str = "SP500_MARKET_REGIME"
    publication_id: str = "SP500_MARKET_REGIME"
    start: str = "1990-01-01"
    strict: bool = True
    min_snapshot_members: int = 450
    max_snapshot_members: int = 550


@dataclass(frozen=True, slots=True)
class ScreeningSettings:
    """Leakage controls and acceptance gates for univariate screening."""

    candidate_registry_path: Path = Path(
        "configs/market_regime_screening_candidates_v2.yaml"
    )
    holdout_start: str = "2022-01-01"
    first_validation_start: str = "1996-01-01"
    validation_years: int = 2
    minimum_train_years: int = 5
    embargo_sessions: int = 60
    minimum_train_rows: int = 252
    minimum_validation_rows: int = 40
    minimum_train_positives: int = 10
    minimum_validation_positives: int = 2
    minimum_fold_count: int = 3
    minimum_event_episodes: int = 30
    minimum_regime_eras: int = 3
    minimum_feature_coverage: float = 0.95
    direction_consistency: float = 0.75
    fdr_q: float = 0.10
    winsor_lower_quantile: float = 0.01
    winsor_upper_quantile: float = 0.99
    signal_quantile: float = 0.90
    ridge_penalty: float = 0.0001
    calibration_bins: int = 10
    bootstrap_iterations: int = 1000
    bootstrap_block_rows: int = 60
    random_seed: int = 20260802
    scan_unregistered: bool = True


@dataclass(frozen=True, slots=True)
class MarketRegimeResearchSettings:
    """Top-level research settings, independent from web feature flags."""

    enabled: bool = True
    primary_symbol: str = "^GSPC"
    end: str = "today"
    raw_root: Path = Path("data/raw/market_regime")
    output_root: Path = Path("outputs/market_regime_research")
    instruments: tuple[PriceInstrumentSettings, ...] = field(
        default_factory=_default_instruments
    )
    labels: LabelSettings = field(default_factory=LabelSettings)
    features: FeatureSettings = field(default_factory=FeatureSettings)
    pit: PITSettings = field(default_factory=PITSettings)
    screening: ScreeningSettings = field(default_factory=ScreeningSettings)

    @property
    def prices_root(self) -> Path:
        return self.raw_root / "prices"

    @property
    def volatility_path(self) -> Path:
        return self.raw_root / "volatility.parquet"

    @property
    def credit_path(self) -> Path:
        return self.raw_root / "credit.parquet"

    @property
    def source_manifest_path(self) -> Path:
        return self.raw_root / "source_manifest.json"

    @property
    def pit_membership_path(self) -> Path:
        """Dedicated PIT publication; never shares the main-factor SP500 file."""
        configured = Path(str(CONFIG.universe.point_in_time.membership_dir))
        root = (
            configured
            if configured.is_absolute()
            else CONFIG.abs_path(str(configured))
        )
        publication_id = safe_path_component(
            self.pit.publication_id,
            label="market-regime PIT publication_id",
        )
        return root / f"{publication_id}.parquet"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _tuple_int(value: Any, default: tuple[int, ...]) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(int(item) for item in value)


def _bool(value: Any, *, default: bool, field_name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true"}:
        return True
    if normalized in {"0", "false"}:
        return False
    raise ValueError(f"{field_name} must be a boolean")


def load_market_regime_research_settings(
    raw_config: Mapping[str, Any] | None = None,
    *,
    raw_root: Path | None = None,
    output_root: Path | None = None,
) -> MarketRegimeResearchSettings:
    """Load settings from YAML-compatible data and enforce safe ranges."""
    root = dict(raw_config) if raw_config is not None else CONFIG.to_dict()
    config = _mapping(root.get("market_regime_research"))
    labels = _mapping(config.get("labels"))
    features = _mapping(config.get("features"))
    pit = _mapping(config.get("point_in_time"))
    screening = _mapping(config.get("screening"))

    raw_instruments = config.get("instruments")
    if isinstance(raw_instruments, list) and raw_instruments:
        parsed_instruments: list[PriceInstrumentSettings] = []
        for position, item in enumerate(raw_instruments):
            if not isinstance(item, Mapping):
                raise ValueError(
                    "market_regime_research.instruments entries must be mappings"
                )
            if "symbol" not in item or "start" not in item:
                raise ValueError(
                    "market_regime_research.instruments "
                    f"entry {position} requires symbol and start"
                )
            parsed_instruments.append(
                PriceInstrumentSettings(
                    symbol=str(item["symbol"]).strip().upper(),
                    start=str(item["start"]),
                    kind=str(item.get("kind", "etf")).strip().lower(),
                )
            )
        instruments = tuple(parsed_instruments)
    else:
        instruments = _default_instruments()

    configured_raw = Path(str(config.get("raw_dir", "data/raw/market_regime")))
    configured_output = Path(
        str(config.get("output_dir", "outputs/market_regime_research"))
    )
    settings = MarketRegimeResearchSettings(
        enabled=_bool(
            config.get("enabled"),
            default=True,
            field_name="market_regime_research.enabled",
        ),
        primary_symbol=str(config.get("primary_symbol", "^GSPC")).strip().upper(),
        end=str(config.get("end", "today")),
        raw_root=Path(raw_root) if raw_root is not None else CONFIG.abs_path(str(configured_raw)),
        output_root=(
            Path(output_root)
            if output_root is not None
            else CONFIG.abs_path(str(configured_output))
        ),
        instruments=instruments,
        labels=LabelSettings(
            horizons=_tuple_int(labels.get("horizons"), (5, 20, 60)),
            volatility_window=int(labels.get("volatility_window", 20)),
            high_lookback=int(labels.get("high_lookback", 252)),
            top_near_high_pct=float(labels.get("top_near_high_pct", 0.03)),
            bottom_min_drawdown_pct=float(
                labels.get("bottom_min_drawdown_pct", 0.10)
            ),
            bottom_drawdown_quantile=float(
                labels.get("bottom_drawdown_quantile", 0.20)
            ),
            bottom_quantile_lookback=int(
                labels.get("bottom_quantile_lookback", 1260)
            ),
            minimum_history=int(labels.get("minimum_history", 252)),
            barrier_vol_multiplier=float(
                labels.get("barrier_vol_multiplier", 1.0)
            ),
            minimum_barrier_pct=float(labels.get("minimum_barrier_pct", 0.02)),
        ),
        features=FeatureSettings(
            moving_average_windows=_tuple_int(
                features.get("moving_average_windows"), (20, 50, 60, 120, 200)
            ),
            breadth_change_windows=_tuple_int(
                features.get("breadth_change_windows"), (5, 20)
            ),
            realized_volatility_windows=_tuple_int(
                features.get("realized_volatility_windows"), (20, 60)
            ),
            correlation_window=int(features.get("correlation_window", 60)),
            correlation_min_members=int(
                features.get("correlation_min_members", 30)
            ),
            momentum_lookback=int(features.get("momentum_lookback", 252)),
            momentum_skip=int(features.get("momentum_skip", 21)),
            momentum_quantile=float(features.get("momentum_quantile", 0.10)),
            min_cross_section_members=int(
                features.get("min_cross_section_members", 30)
            ),
            min_cross_section_coverage=float(
                features.get("min_cross_section_coverage", 0.95)
            ),
        ),
        pit=PITSettings(
            universe=str(pit.get("universe", "SP500")).strip().upper(),
            data_universe=str(
                pit.get("data_universe", "SP500_MARKET_REGIME")
            ).strip().upper(),
            publication_id=str(
                pit.get("publication_id", "SP500_MARKET_REGIME")
            ).strip().upper(),
            start=str(pit.get("start", "1990-01-01")),
            strict=_bool(
                pit.get("strict"),
                default=True,
                field_name="market_regime_research.point_in_time.strict",
            ),
            min_snapshot_members=int(pit.get("min_snapshot_members", 450)),
            max_snapshot_members=int(pit.get("max_snapshot_members", 550)),
        ),
        screening=ScreeningSettings(
            candidate_registry_path=CONFIG.abs_path(
                str(
                    screening.get(
                        "candidate_registry",
                        "configs/market_regime_screening_candidates_v2.yaml",
                    )
                )
            ),
            holdout_start=str(screening.get("holdout_start", "2022-01-01")),
            first_validation_start=str(
                screening.get("first_validation_start", "1996-01-01")
            ),
            validation_years=int(screening.get("validation_years", 2)),
            minimum_train_years=int(screening.get("minimum_train_years", 5)),
            embargo_sessions=int(screening.get("embargo_sessions", 60)),
            minimum_train_rows=int(screening.get("minimum_train_rows", 252)),
            minimum_validation_rows=int(
                screening.get("minimum_validation_rows", 40)
            ),
            minimum_train_positives=int(
                screening.get("minimum_train_positives", 10)
            ),
            minimum_validation_positives=int(
                screening.get("minimum_validation_positives", 2)
            ),
            minimum_fold_count=int(screening.get("minimum_fold_count", 3)),
            minimum_event_episodes=int(
                screening.get("minimum_event_episodes", 30)
            ),
            minimum_regime_eras=int(
                screening.get("minimum_regime_eras", 3)
            ),
            minimum_feature_coverage=float(
                screening.get("minimum_feature_coverage", 0.95)
            ),
            direction_consistency=float(
                screening.get("direction_consistency", 0.75)
            ),
            fdr_q=float(screening.get("fdr_q", 0.10)),
            winsor_lower_quantile=float(
                screening.get("winsor_lower_quantile", 0.01)
            ),
            winsor_upper_quantile=float(
                screening.get("winsor_upper_quantile", 0.99)
            ),
            signal_quantile=float(screening.get("signal_quantile", 0.90)),
            ridge_penalty=float(screening.get("ridge_penalty", 0.0001)),
            calibration_bins=int(screening.get("calibration_bins", 10)),
            bootstrap_iterations=int(
                screening.get("bootstrap_iterations", 1000)
            ),
            bootstrap_block_rows=int(
                screening.get(
                    "bootstrap_block_rows",
                    screening.get("bootstrap_block_sessions", 60),
                )
            ),
            random_seed=int(screening.get("random_seed", 20260802)),
            scan_unregistered=_bool(
                screening.get("scan_unregistered"),
                default=True,
                field_name="market_regime_research.screening.scan_unregistered",
            ),
        ),
    )
    _validate(settings)
    return settings


def _validate(settings: MarketRegimeResearchSettings) -> None:
    symbols = [item.symbol for item in settings.instruments]
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("market_regime_research.instruments must be unique")
    for item in settings.instruments:
        canonical_ticker(item.symbol, label="market instrument symbol")
        parse_date_str(item.start)
    end = parse_date_str(settings.end)
    if any(parse_date_str(item.start) > end for item in settings.instruments):
        raise ValueError("instrument start cannot be after configured end")
    if settings.primary_symbol not in symbols:
        raise ValueError("primary_symbol must appear in instruments")
    if any(item.kind not in {"index", "etf"} for item in settings.instruments):
        raise ValueError("instrument kind must be index or etf")
    if not settings.labels.horizons or any(
        horizon < 1 for horizon in settings.labels.horizons
    ):
        raise ValueError("label horizons must be positive")
    if len(set(settings.labels.horizons)) != len(settings.labels.horizons):
        raise ValueError("label horizons must be unique")
    if settings.labels.volatility_window < 2:
        raise ValueError("volatility_window must be at least 2")
    if settings.labels.minimum_history < 2:
        raise ValueError("minimum_history must be at least 2")
    if settings.labels.high_lookback < settings.labels.minimum_history:
        raise ValueError("high_lookback cannot be shorter than minimum_history")
    if settings.labels.bottom_quantile_lookback < settings.labels.minimum_history:
        raise ValueError(
            "bottom_quantile_lookback cannot be shorter than minimum_history"
        )
    if not 0 <= settings.labels.top_near_high_pct < 1:
        raise ValueError("top_near_high_pct must be in [0, 1)")
    if not 0 < settings.labels.bottom_min_drawdown_pct < 1:
        raise ValueError("bottom_min_drawdown_pct must be in (0, 1)")
    if not 0 < settings.labels.bottom_drawdown_quantile < 1:
        raise ValueError("bottom_drawdown_quantile must be in (0, 1)")
    if settings.labels.barrier_vol_multiplier <= 0:
        raise ValueError("barrier_vol_multiplier must be positive")
    if not 0 < settings.labels.minimum_barrier_pct < 1:
        raise ValueError("minimum_barrier_pct must be in (0, 1)")
    if not settings.features.moving_average_windows or any(
        window < 1 for window in settings.features.moving_average_windows
    ):
        raise ValueError("moving_average_windows must be positive")
    if len(set(settings.features.moving_average_windows)) != len(
        settings.features.moving_average_windows
    ):
        raise ValueError("moving_average_windows must be unique")
    if not settings.features.breadth_change_windows or any(
        window < 1 for window in settings.features.breadth_change_windows
    ):
        raise ValueError("breadth_change_windows must be positive")
    if len(set(settings.features.breadth_change_windows)) != len(
        settings.features.breadth_change_windows
    ):
        raise ValueError("breadth_change_windows must be unique")
    if not settings.features.realized_volatility_windows or any(
        window < 2 for window in settings.features.realized_volatility_windows
    ):
        raise ValueError("realized_volatility_windows must be at least 2")
    if len(set(settings.features.realized_volatility_windows)) != len(
        settings.features.realized_volatility_windows
    ):
        raise ValueError("realized_volatility_windows must be unique")
    if settings.features.correlation_window < 2:
        raise ValueError("correlation_window must be at least 2")
    if settings.features.correlation_min_members < 2:
        raise ValueError("correlation_min_members must be at least 2")
    if settings.features.min_cross_section_members < 2:
        raise ValueError("min_cross_section_members must be at least 2")
    if not 0 < settings.features.min_cross_section_coverage <= 1:
        raise ValueError("min_cross_section_coverage must be in (0, 1]")
    if settings.features.momentum_skip < 1:
        raise ValueError("momentum_skip must be positive")
    if settings.features.momentum_lookback <= settings.features.momentum_skip:
        raise ValueError("momentum_lookback must be greater than momentum_skip")
    if not 0 < settings.features.momentum_quantile < 0.5:
        raise ValueError("momentum_quantile must be in (0, 0.5)")
    safe_path_component(settings.pit.universe, label="PIT universe")
    safe_path_component(
        settings.pit.data_universe,
        label="market-regime data universe",
    )
    safe_path_component(
        settings.pit.publication_id,
        label="market-regime PIT publication_id",
    )
    if settings.pit.data_universe == settings.pit.universe:
        raise ValueError(
            "market-regime data_universe must be isolated from the main universe"
        )
    if settings.pit.publication_id == settings.pit.universe:
        raise ValueError(
            "market-regime PIT publication_id must be isolated from the main universe"
        )
    parse_date_str(settings.pit.start)
    if parse_date_str(settings.pit.start) > end:
        raise ValueError("PIT start cannot be after configured end")
    if settings.pit.min_snapshot_members < 1:
        raise ValueError("PIT min_snapshot_members must be positive")
    if settings.pit.min_snapshot_members > settings.pit.max_snapshot_members:
        raise ValueError("PIT min_snapshot_members cannot exceed max_snapshot_members")
    parse_date_str(settings.screening.holdout_start)
    if settings.screening.embargo_sessions < max(settings.labels.horizons):
        raise ValueError(
            "screening embargo_sessions must be at least the maximum label horizon "
            "to keep outcome windows outside validation and sealed holdout data"
        )
    parse_date_str(settings.screening.first_validation_start)
    if (
        parse_date_str(settings.screening.first_validation_start)
        >= parse_date_str(settings.screening.holdout_start)
    ):
        raise ValueError("screening first_validation_start must precede holdout_start")
    for value, field_name in (
        (settings.screening.validation_years, "validation_years"),
        (settings.screening.minimum_train_years, "minimum_train_years"),
        (settings.screening.embargo_sessions, "embargo_sessions"),
        (settings.screening.minimum_train_rows, "minimum_train_rows"),
        (settings.screening.minimum_validation_rows, "minimum_validation_rows"),
        (settings.screening.minimum_train_positives, "minimum_train_positives"),
        (
            settings.screening.minimum_validation_positives,
            "minimum_validation_positives",
        ),
        (settings.screening.minimum_fold_count, "minimum_fold_count"),
        (settings.screening.minimum_event_episodes, "minimum_event_episodes"),
        (settings.screening.minimum_regime_eras, "minimum_regime_eras"),
        (settings.screening.calibration_bins, "calibration_bins"),
        (settings.screening.bootstrap_iterations, "bootstrap_iterations"),
        (
            settings.screening.bootstrap_block_rows,
            "bootstrap_block_rows",
        ),
    ):
        if value < 1:
            raise ValueError(f"screening {field_name} must be positive")
    if not 0.5 <= settings.screening.direction_consistency <= 1:
        raise ValueError("screening direction_consistency must be in [0.5, 1]")
    if not 0 < settings.screening.minimum_feature_coverage <= 1:
        raise ValueError("screening minimum_feature_coverage must be in (0, 1]")
    if not 0 < settings.screening.fdr_q < 1:
        raise ValueError("screening fdr_q must be in (0, 1)")
    if not (
        0
        <= settings.screening.winsor_lower_quantile
        < settings.screening.winsor_upper_quantile
        <= 1
    ):
        raise ValueError("screening winsor quantiles are invalid")
    if not 0.5 < settings.screening.signal_quantile < 1:
        raise ValueError("screening signal_quantile must be in (0.5, 1)")
    if settings.screening.ridge_penalty < 0:
        raise ValueError("screening ridge_penalty cannot be negative")
    candidate_registry = settings.screening.candidate_registry_path
    if str(candidate_registry).strip() in {"", ".", "..", "/"}:
        raise ValueError("screening candidate_registry is unsafe")
    for path, field_name in (
        (settings.raw_root, "raw_dir"),
        (settings.output_root, "output_dir"),
    ):
        if str(path).strip() in {"", ".", "..", "/"}:
            raise ValueError(f"market_regime_research.{field_name} is unsafe")


__all__ = [
    "FeatureSettings",
    "LabelSettings",
    "MarketRegimeResearchSettings",
    "PITSettings",
    "PriceInstrumentSettings",
    "ScreeningSettings",
    "load_market_regime_research_settings",
]
