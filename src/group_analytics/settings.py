"""Validated, lazily loaded configuration for group analytics."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from pathlib import Path
from typing import Any, Mapping

from src.config import CONFIG


@dataclass(frozen=True, slots=True)
class ClassificationSettings:
    default_taxonomy: str = "FMP"
    default_level: str = "sector"
    issuer_override_path: str = "configs/classifications/issuer_overrides.yaml"
    group_id_mapping_path: str = "configs/classifications/fmp_group_ids.yaml"
    counting_unit: str = "security_with_overrides"
    include_etfs: bool = False


@dataclass(frozen=True, slots=True)
class DailyReturnSettings:
    headline_method: str = "ROBUST_EW"
    winsorize_method: str = "mad"
    winsorize_n: float = 3.0
    min_members_for_winsorize: int = 5
    unchanged_band_bps: float = 1.0


@dataclass(frozen=True, slots=True)
class RankingSettings:
    top_n: int = 5
    bottom_n: int = 5
    min_members: int = 5
    min_count_coverage: float = 0.80
    min_freshness_coverage: float = 0.80
    allowed_quality_grades: tuple[str, ...] = ("A", "B")
    single_name_concentration_warning: float = 0.35


@dataclass(frozen=True, slots=True)
class InputSettings:
    min_return_coverage: float = 0.80
    require_benchmark: bool = False


@dataclass(frozen=True, slots=True)
class FreshnessSettings:
    eod_publish_sla_minutes: int = 180


@dataclass(frozen=True, slots=True)
class GroupAnalyticsSettings:
    enabled: bool = False
    web_enabled: bool = False
    default_universe: str = "SP500"
    universes: tuple[str, ...] = ("SP500",)
    output_subdir: str = "universes"
    benchmark: str = "SPY"
    output_root: Path = Path("outputs")
    classification: ClassificationSettings = field(default_factory=ClassificationSettings)
    daily_return: DailyReturnSettings = field(default_factory=DailyReturnSettings)
    ranking: RankingSettings = field(default_factory=RankingSettings)
    inputs: InputSettings = field(default_factory=InputSettings)
    freshness: FreshnessSettings = field(default_factory=FreshnessSettings)

    @property
    def artifact_root(self) -> Path:
        return self.output_root / self.output_subdir

    @property
    def issuer_override_path(self) -> Path:
        return CONFIG.abs_path(self.classification.issuer_override_path)

    @property
    def group_id_mapping_path(self) -> Path:
        return CONFIG.abs_path(self.classification.group_id_mapping_path)

    def algorithm_config(self) -> dict[str, Any]:
        """Only values that can change calculated numbers or eligibility."""
        return {
            "benchmark": self.benchmark,
            "classification": {
                "counting_unit": self.classification.counting_unit,
                "include_etfs": self.classification.include_etfs,
            },
            "daily_return": asdict(self.daily_return),
            "ranking": asdict(self.ranking),
            "inputs": asdict(self.inputs),
        }

    def runtime_config(self) -> dict[str, Any]:
        return {
            "output_root": str(self.output_root),
            "output_subdir": self.output_subdir,
            "issuer_override_path": str(self.issuer_override_path),
            "group_id_mapping_path": str(self.group_id_mapping_path),
            "freshness": asdict(self.freshness),
        }


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _tuple_str(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return default
    return tuple(str(item) for item in value)


def _bool_value(value: Any, *, default: bool, field: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise ValueError(f"{field} must be a boolean")


def load_group_analytics_settings(
    raw_config: Mapping[str, Any] | None = None,
    *,
    output_root: Path | None = None,
) -> GroupAnalyticsSettings:
    """Load settings without importing group analytics from the global config layer."""
    root = dict(raw_config) if raw_config is not None else CONFIG.to_dict()
    cfg = _dict(root.get("group_analytics"))
    classification = _dict(cfg.get("classification"))
    daily = _dict(cfg.get("daily_return"))
    ranking = _dict(cfg.get("ranking"))
    inputs = _dict(cfg.get("inputs"))
    freshness = _dict(cfg.get("freshness"))
    benchmarks = _dict(cfg.get("benchmarks"))

    if output_root is None:
        storage = _dict(root.get("storage"))
        webapp = _dict(root.get("webapp"))
        configured = storage.get("output_dir") or webapp.get("output_dir") or "outputs"
        output_root = CONFIG.abs_path(str(configured))

    settings = GroupAnalyticsSettings(
        enabled=_bool_value(
            os.environ.get("GROUP_ANALYTICS_ENABLED", cfg.get("enabled")),
            default=False,
            field="group_analytics.enabled",
        ),
        web_enabled=_bool_value(
            os.environ.get("GROUP_ANALYTICS_WEB_ENABLED", cfg.get("web_enabled")),
            default=False,
            field="group_analytics.web_enabled",
        ),
        default_universe=str(cfg.get("default_universe", "SP500")).upper(),
        universes=tuple(item.upper() for item in _tuple_str(cfg.get("universes"), ("SP500",))),
        output_subdir=str(cfg.get("output_subdir", "universes")),
        benchmark=str(benchmarks.get("SP500", "SPY")).upper(),
        output_root=Path(output_root),
        classification=ClassificationSettings(
            default_taxonomy=str(classification.get("default_taxonomy", "FMP")).upper(),
            default_level=str(classification.get("default_level", "sector")).lower(),
            issuer_override_path=str(
                classification.get(
                    "issuer_override_path",
                    "configs/classifications/issuer_overrides.yaml",
                )
            ),
            group_id_mapping_path=str(
                classification.get(
                    "group_id_mapping_path",
                    "configs/classifications/fmp_group_ids.yaml",
                )
            ),
            counting_unit=str(classification.get("counting_unit", "security_with_overrides")),
            include_etfs=_bool_value(
                classification.get("include_etfs"),
                default=False,
                field="group_analytics.classification.include_etfs",
            ),
        ),
        daily_return=DailyReturnSettings(
            headline_method=str(daily.get("headline_method", "ROBUST_EW")).upper(),
            winsorize_method=str(daily.get("winsorize_method", "mad")).lower(),
            winsorize_n=float(daily.get("winsorize_n", 3.0)),
            min_members_for_winsorize=int(daily.get("min_members_for_winsorize", 5)),
            unchanged_band_bps=float(daily.get("unchanged_band_bps", 1.0)),
        ),
        ranking=RankingSettings(
            top_n=int(ranking.get("top_n", 5)),
            bottom_n=int(ranking.get("bottom_n", 5)),
            min_members=int(ranking.get("min_members", 5)),
            min_count_coverage=float(ranking.get("min_count_coverage", 0.80)),
            min_freshness_coverage=float(ranking.get("min_freshness_coverage", 0.80)),
            allowed_quality_grades=tuple(
                item.upper()
                for item in _tuple_str(ranking.get("allowed_quality_grades"), ("A", "B"))
            ),
            single_name_concentration_warning=float(
                ranking.get("single_name_concentration_warning", 0.35)
            ),
        ),
        inputs=InputSettings(
            min_return_coverage=float(inputs.get("min_return_coverage", 0.80)),
            require_benchmark=_bool_value(
                inputs.get("require_benchmark"),
                default=False,
                field="group_analytics.inputs.require_benchmark",
            ),
        ),
        freshness=FreshnessSettings(
            eod_publish_sla_minutes=int(freshness.get("eod_publish_sla_minutes", 180)),
        ),
    )
    _validate(settings)
    return settings


def _validate(settings: GroupAnalyticsSettings) -> None:
    output_subdir = Path(settings.output_subdir)
    if (
        settings.output_subdir in {"", ".", ".."}
        or output_subdir.is_absolute()
        or not output_subdir.parts
        or any(part in {"", ".", ".."} for part in output_subdir.parts)
    ):
        raise ValueError("group_analytics.output_subdir must be a safe relative directory")
    if settings.classification.counting_unit != "security_with_overrides":
        raise ValueError("Stage 1 supports counting_unit=security_with_overrides only")
    if settings.daily_return.headline_method != "ROBUST_EW":
        raise ValueError("Stage 1 requires headline_method=ROBUST_EW")
    if settings.daily_return.winsorize_method != "mad":
        raise ValueError("Stage 1 requires winsorize_method=mad")
    if settings.daily_return.min_members_for_winsorize < 1:
        raise ValueError("min_members_for_winsorize must be positive")
    if settings.ranking.min_members < 1:
        raise ValueError("ranking.min_members must be positive")
    for value in (
        settings.ranking.min_count_coverage,
        settings.ranking.min_freshness_coverage,
        settings.inputs.min_return_coverage,
    ):
        if not 0 <= value <= 1:
            raise ValueError("coverage thresholds must be in [0, 1]")


__all__ = [
    "ClassificationSettings",
    "DailyReturnSettings",
    "FreshnessSettings",
    "GroupAnalyticsSettings",
    "InputSettings",
    "RankingSettings",
    "load_group_analytics_settings",
]
