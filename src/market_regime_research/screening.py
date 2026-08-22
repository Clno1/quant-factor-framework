"""Leakage-controlled univariate screening for market turning-point features.

The module deliberately stops at candidate screening.  It does not combine
features, tune a production model, or inspect the sealed holdout period.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.special import expit
from scipy.stats import mannwhitneyu, rankdata
import yaml

from src.market_regime_research.models import DataContractError
from src.market_regime_research.settings import ScreeningSettings

SIDES = ("top", "bottom")
PATH_COLUMNS = ("forward_return", "future_mfe", "future_mae")


@dataclass(frozen=True, slots=True)
class ScreeningCandidate:
    """One feature, event side, and forecast horizon to be tested once."""

    candidate_id: str
    feature_name: str
    side: str
    horizon: int
    expected_direction: int | None
    family: str
    mechanism: str
    overlap_reason: str
    hypothesis_tier: str
    registration_source: str

    @property
    def is_confirmatory(self) -> bool:
        return self.hypothesis_tier == "confirmatory"

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "feature_name": self.feature_name,
            "side": self.side,
            "horizon": self.horizon,
            "expected_direction": _direction_name(self.expected_direction),
            "family": self.family,
            "mechanism": self.mechanism,
            "overlap_reason": self.overlap_reason,
            "hypothesis_tier": self.hypothesis_tier,
            "registration_source": self.registration_source,
        }


@dataclass(slots=True)
class ScreeningOutputs:
    """All tabular outputs from one effectiveness screen."""

    candidate_registry: pd.DataFrame
    event_studies: pd.DataFrame
    fold_results: pd.DataFrame
    predictions: pd.DataFrame
    scorecard: pd.DataFrame
    summary: dict[str, Any]


def _direction_name(value: int | None) -> str | None:
    if value is None:
        return None
    return "higher" if value > 0 else "lower"


def _direction_value(value: Any) -> int:
    normalized = str(value).strip().casefold()
    if normalized == "higher":
        return 1
    if normalized == "lower":
        return -1
    raise DataContractError("expected_direction must be 'higher' or 'lower'")


def _require_nonempty_text(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DataContractError(f"Candidate {field_name} must be non-empty")
    return text


def load_candidate_registry(
    path: Path,
    feature_registry: pd.DataFrame,
    *,
    horizons: Sequence[int],
    scan_unregistered: bool,
) -> tuple[list[ScreeningCandidate], dict[str, Any]]:
    """Load frozen hypotheses and optionally append every unregistered scan.

    Registered candidates keep their stated economic direction.  Every missing
    feature/side/horizon tuple receives an exploratory record whose direction
    will be learned once from the first eligible training fold.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Candidate registry not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise DataContractError("Candidate registry root must be a mapping")
    hypotheses = payload.get("hypotheses")
    if not isinstance(hypotheses, list):
        raise DataContractError("Candidate registry hypotheses must be a list")

    required_registry_columns = {"feature_name", "group"}
    missing_columns = required_registry_columns.difference(feature_registry.columns)
    if missing_columns:
        raise DataContractError(
            "Feature registry is missing columns: "
            + ", ".join(sorted(missing_columns))
        )
    if feature_registry["feature_name"].duplicated().any():
        raise DataContractError("Feature registry contains duplicate feature names")
    feature_groups = feature_registry.set_index("feature_name")["group"].to_dict()
    feature_names = set(feature_groups)
    allowed_horizons = {int(item) for item in horizons}
    if not allowed_horizons:
        raise DataContractError("At least one label horizon is required")

    candidates: list[ScreeningCandidate] = []
    seen_base_ids: set[str] = set()
    seen_keys: set[tuple[str, str, int]] = set()
    for position, raw in enumerate(hypotheses):
        if not isinstance(raw, Mapping):
            raise DataContractError(
                f"Candidate registry entry {position} must be a mapping"
            )
        base_id = _require_nonempty_text(
            raw.get("candidate_id"), field_name="candidate_id"
        )
        if base_id in seen_base_ids:
            raise DataContractError(f"Duplicate candidate_id: {base_id}")
        seen_base_ids.add(base_id)
        feature_name = _require_nonempty_text(
            raw.get("feature_name"), field_name="feature_name"
        )
        if feature_name not in feature_names:
            raise DataContractError(
                f"Candidate {base_id} references unknown feature {feature_name}"
            )
        side = str(raw.get("side", "")).strip().casefold()
        if side not in SIDES:
            raise DataContractError(f"Candidate {base_id} has invalid side {side!r}")
        raw_horizons = raw.get("horizons")
        if not isinstance(raw_horizons, list) or not raw_horizons:
            raise DataContractError(f"Candidate {base_id} requires horizons")
        candidate_horizons = [int(item) for item in raw_horizons]
        if len(candidate_horizons) != len(set(candidate_horizons)):
            raise DataContractError(
                f"Candidate {base_id} contains duplicate horizons"
            )
        unsupported = set(candidate_horizons).difference(allowed_horizons)
        if unsupported:
            raise DataContractError(
                f"Candidate {base_id} uses unavailable horizons {sorted(unsupported)}"
            )
        expected_direction = _direction_value(raw.get("expected_direction"))
        family = _require_nonempty_text(raw.get("family"), field_name="family")
        mechanism = _require_nonempty_text(
            raw.get("mechanism"), field_name="mechanism"
        )
        overlap_reason = _require_nonempty_text(
            raw.get("overlap_reason"), field_name="overlap_reason"
        )
        for horizon in candidate_horizons:
            key = (feature_name, side, horizon)
            if key in seen_keys:
                raise DataContractError(
                    "Duplicate registered feature/side/horizon hypothesis: "
                    f"{feature_name}/{side}/{horizon}"
                )
            seen_keys.add(key)
            candidates.append(
                ScreeningCandidate(
                    candidate_id=f"{base_id}__{horizon}d",
                    feature_name=feature_name,
                    side=side,
                    horizon=horizon,
                    expected_direction=expected_direction,
                    family=family,
                    mechanism=mechanism,
                    overlap_reason=overlap_reason,
                    hypothesis_tier="confirmatory",
                    registration_source=str(path),
                )
            )

    if scan_unregistered:
        for feature_name in feature_registry["feature_name"].tolist():
            for side in SIDES:
                for horizon in sorted(allowed_horizons):
                    key = (feature_name, side, horizon)
                    if key in seen_keys:
                        continue
                    candidates.append(
                        ScreeningCandidate(
                            candidate_id=(
                                f"explore__{feature_name}__{side}__{horizon}d"
                            ),
                            feature_name=feature_name,
                            side=side,
                            horizon=horizon,
                            expected_direction=None,
                            family=str(feature_groups[feature_name]),
                            mechanism=(
                                "Unregistered broad P0 scan; cannot enter the "
                                "confirmatory shortlist."
                            ),
                            overlap_reason=(
                                "Retained so every attempted feature/outcome "
                                "combination is visible to multiple-testing controls."
                            ),
                            hypothesis_tier="exploratory",
                            registration_source="generated_full_scan",
                        )
                    )
    if not candidates:
        raise DataContractError("Candidate registry expanded to zero tests")
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise DataContractError("Expanded candidate identifiers are not unique")

    metadata = {
        "registry_version": str(payload.get("registry_version", "")),
        "frozen_at": str(payload.get("frozen_at", "")),
        "registration_type": str(payload.get("registration_type", "")),
        "retrospective_holdout_start": str(
            payload.get("retrospective_holdout_start", "")
        ),
        "prospective_shadow_start": str(
            payload.get("prospective_shadow_start", "")
        ),
        "registered_base_hypotheses": len(hypotheses),
        "confirmatory_tests": sum(item.is_confirmatory for item in candidates),
        "exploratory_tests": sum(not item.is_confirmatory for item in candidates),
        "total_tests": len(candidates),
    }
    return candidates, metadata


def validation_windows(
    sessions: pd.DatetimeIndex,
    settings: ScreeningSettings,
) -> tuple[list[tuple[str, pd.Timestamp, pd.Timestamp]], pd.Timestamp]:
    """Create shared calendar folds and a purged development cutoff.

    The final development feature date is at least ``embargo_sessions`` before
    the sealed holdout.  Consequently, even the longest forward label cannot
    consume prices from the holdout period.
    """
    index = _validate_sessions(sessions)
    holdout_start = pd.Timestamp(settings.holdout_start).normalize()
    holdout_position = int(index.searchsorted(holdout_start, side="left"))
    development_position = holdout_position - settings.embargo_sessions - 1
    if development_position < 0:
        raise DataContractError(
            "Not enough sessions before holdout_start and embargo"
        )
    development_end = index[development_position]
    first_start = pd.Timestamp(settings.first_validation_start).normalize()
    if first_start > development_end:
        raise DataContractError("No validation window exists before the holdout")

    windows: list[tuple[str, pd.Timestamp, pd.Timestamp]] = []
    start = first_start
    while start <= development_end:
        nominal_end = start + pd.DateOffset(
            years=settings.validation_years
        ) - pd.Timedelta(days=1)
        end = min(nominal_end, development_end)
        windows.append((f"wf_{start.year}_{end.year}", start, end))
        start = start + pd.DateOffset(years=settings.validation_years)
    return windows, development_end


def _validate_sessions(sessions: pd.DatetimeIndex) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(sessions).tz_localize(None).normalize()
    if index.empty or index.has_duplicates or not index.is_monotonic_increasing:
        raise DataContractError("Sessions must be non-empty, unique, and increasing")
    return index


def _purged_train_end(
    sessions: pd.DatetimeIndex,
    validation_start: pd.Timestamp,
    embargo_sessions: int,
) -> pd.Timestamp | None:
    validation_position = int(
        sessions.searchsorted(validation_start, side="left")
    )
    train_position = validation_position - embargo_sessions - 1
    return sessions[train_position] if train_position >= 0 else None


def _fit_logistic(
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    ridge_penalty: float,
) -> tuple[float, float]:
    """Fit a deterministic one-variable ridge logistic model."""
    prevalence = float(np.mean(y_train))
    if not 0 < prevalence < 1:
        raise DataContractError("Logistic training data must contain both classes")
    initial = np.array(
        [np.log(prevalence / (1.0 - prevalence)), 0.0],
        dtype=float,
    )

    def objective(parameters: np.ndarray) -> tuple[float, np.ndarray]:
        linear = parameters[0] + parameters[1] * x_train
        losses = np.logaddexp(0.0, linear) - y_train * linear
        probability = expit(linear)
        residual = probability - y_train
        value = float(
            losses.mean() + 0.5 * ridge_penalty * parameters[1] ** 2
        )
        gradient = np.array(
            [
                residual.mean(),
                np.mean(residual * x_train)
                + ridge_penalty * parameters[1],
            ],
            dtype=float,
        )
        return value, gradient

    result = minimize(
        objective,
        initial,
        method="L-BFGS-B",
        jac=True,
        options={"maxiter": 500, "ftol": 1e-12},
    )
    if not result.success or not np.isfinite(result.x).all():
        raise DataContractError(
            f"Logistic optimization failed: {result.message}"
        )
    return float(result.x[0]), float(result.x[1])


def _average_precision(y_true: np.ndarray, probability: np.ndarray) -> float:
    positives = int(np.sum(y_true))
    if positives == 0:
        return float("nan")
    order = np.argsort(-probability, kind="mergesort")
    ordered = y_true[order]
    precision = np.cumsum(ordered) / np.arange(1, len(ordered) + 1)
    return float(np.sum(precision * ordered) / positives)


def _roc_auc(y_true: np.ndarray, score: np.ndarray) -> float:
    positive = y_true == 1
    negative = ~positive
    positive_count = int(positive.sum())
    negative_count = int(negative.sum())
    if positive_count == 0 or negative_count == 0:
        return float("nan")
    ranks = rankdata(score, method="average")
    rank_sum = float(ranks[positive].sum())
    statistic = rank_sum - positive_count * (positive_count + 1) / 2
    return statistic / (positive_count * negative_count)


def _calibration_error(
    y_true: np.ndarray,
    probability: np.ndarray,
    *,
    bins: int,
) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    assignments = np.clip(
        np.searchsorted(edges, probability, side="right") - 1,
        0,
        bins - 1,
    )
    error = 0.0
    for bin_number in range(bins):
        mask = assignments == bin_number
        if not mask.any():
            continue
        error += (
            float(mask.mean())
            * abs(float(probability[mask].mean()) - float(y_true[mask].mean()))
        )
    return error


def _classification_metrics(
    y_true: np.ndarray,
    probability: np.ndarray,
    baseline_probability: np.ndarray,
    *,
    calibration_bins: int,
) -> dict[str, float]:
    model_brier = float(np.mean((y_true - probability) ** 2))
    baseline_brier = float(np.mean((y_true - baseline_probability) ** 2))
    brier_skill = (
        1.0 - model_brier / baseline_brier
        if baseline_brier > 0
        else float("nan")
    )
    prevalence = float(y_true.mean())
    average_precision = _average_precision(y_true, probability)
    return {
        "prevalence": prevalence,
        "average_precision": average_precision,
        "pr_auc_lift": average_precision - prevalence,
        "roc_auc": _roc_auc(y_true, probability),
        "brier": model_brier,
        "baseline_brier": baseline_brier,
        "brier_skill": brier_skill,
        "calibration_error": _calibration_error(
            y_true,
            probability,
            bins=calibration_bins,
        ),
    }


def _positive_episodes(
    dates: pd.DatetimeIndex,
    values: np.ndarray,
    *,
    sessions: pd.DatetimeIndex,
    max_gap_sessions: int,
) -> list[np.ndarray]:
    """Group overlapping positive labels or alarms into independent episodes."""
    positive_dates = pd.DatetimeIndex(dates[np.asarray(values, dtype=bool)])
    if positive_dates.empty:
        return []
    positions = sessions.get_indexer(positive_dates)
    if (positions < 0).any():
        raise DataContractError("Episode dates are absent from the session index")
    episodes: list[list[int]] = [[0]]
    for item in range(1, len(positive_dates)):
        if positions[item] - positions[item - 1] > max_gap_sessions:
            episodes.append([item])
        else:
            episodes[-1].append(item)
    return [positive_dates[np.asarray(items, dtype=int)].to_numpy() for items in episodes]


def _episode_metrics(
    dates: pd.DatetimeIndex,
    y_true: np.ndarray,
    signal: np.ndarray,
    *,
    sessions: pd.DatetimeIndex,
    horizon: int,
) -> dict[str, float | int]:
    positive_episodes = _positive_episodes(
        dates,
        y_true == 1,
        sessions=sessions,
        max_gap_sessions=horizon,
    )
    signal_episodes = _positive_episodes(
        dates,
        signal,
        sessions=sessions,
        max_gap_sessions=horizon,
    )
    positive_dates = set(dates[y_true == 1].to_numpy())
    signal_dates = set(dates[signal].to_numpy())
    captured_positive = sum(
        bool(set(episode).intersection(signal_dates))
        for episode in positive_episodes
    )
    correct_signal = sum(
        bool(set(episode).intersection(positive_dates))
        for episode in signal_episodes
    )
    false_signal = len(signal_episodes) - correct_signal
    span_years = max(
        (dates.max() - dates.min()).days / 365.25 if len(dates) > 1 else 0.0,
        1.0 / 252.0,
    )
    return {
        "positive_event_episodes": len(positive_episodes),
        "signal_episodes": len(signal_episodes),
        "event_precision": (
            correct_signal / len(signal_episodes)
            if signal_episodes
            else float("nan")
        ),
        "event_recall": (
            captured_positive / len(positive_episodes)
            if positive_episodes
            else float("nan")
        ),
        "false_alarm_episodes_per_year": false_signal / span_years,
    }


def _episode_starts(
    dates: pd.DatetimeIndex,
    y_true: np.ndarray,
    *,
    sessions: pd.DatetimeIndex,
    horizon: int,
) -> pd.DatetimeIndex:
    episodes = _positive_episodes(
        dates,
        y_true == 1,
        sessions=sessions,
        max_gap_sessions=horizon,
    )
    return pd.DatetimeIndex([pd.Timestamp(episode[0]) for episode in episodes])


def _decluster_for_rank_test(
    frame: pd.DataFrame,
    *,
    sessions: pd.DatetimeIndex,
    horizon: int,
) -> pd.DataFrame:
    """Keep one positive per event and spaced controls for rank inference."""
    y_true = frame["y"].to_numpy(dtype=int)
    positive_episodes = _positive_episodes(
        pd.DatetimeIndex(frame.index),
        y_true == 1,
        sessions=sessions,
        max_gap_sessions=horizon,
    )
    positive_dates = [pd.Timestamp(episode[0]) for episode in positive_episodes]
    negative_dates = pd.DatetimeIndex(frame.index[y_true == 0])
    negative_positions = sessions.get_indexer(negative_dates)
    selected_negative: list[pd.Timestamp] = []
    last_position: int | None = None
    for date, position in zip(negative_dates, negative_positions, strict=True):
        if last_position is None or position - last_position >= horizon:
            selected_negative.append(pd.Timestamp(date))
            last_position = int(position)
    selected = pd.DatetimeIndex(positive_dates + selected_negative).sort_values()
    return frame.loc[selected]


def _rank_test(
    frame: pd.DataFrame,
    *,
    expected_direction: int | None,
    confirmatory: bool,
    sessions: pd.DatetimeIndex,
    horizon: int,
) -> dict[str, float | int]:
    sample = _decluster_for_rank_test(
        frame,
        sessions=sessions,
        horizon=horizon,
    )
    positive = sample.loc[sample["y"] == 1, "x"].to_numpy(dtype=float)
    negative = sample.loc[sample["y"] == 0, "x"].to_numpy(dtype=float)
    if not len(positive) or not len(negative):
        return {
            "rank_test_p_value": float("nan"),
            "rank_effect_auc": float("nan"),
            "rank_test_positive_rows": len(positive),
            "rank_test_negative_rows": len(negative),
        }
    alternative = "two-sided"
    if confirmatory and expected_direction is not None:
        alternative = "greater" if expected_direction > 0 else "less"
    result = mannwhitneyu(
        positive,
        negative,
        alternative=alternative,
        method="asymptotic",
    )
    raw_auc = _roc_auc(
        np.concatenate(
            [np.ones(len(positive), dtype=int), np.zeros(len(negative), dtype=int)]
        ),
        np.concatenate([positive, negative]),
    )
    oriented_auc = (
        raw_auc
        if expected_direction is None or expected_direction > 0
        else 1.0 - raw_auc
    )
    return {
        "rank_test_p_value": float(result.pvalue),
        "rank_effect_auc": float(oriented_auc),
        "rank_test_positive_rows": len(positive),
        "rank_test_negative_rows": len(negative),
    }


def benjamini_hochberg(values: Iterable[float]) -> np.ndarray:
    """Return BH-adjusted q-values while preserving NaN positions."""
    p_values = np.asarray(list(values), dtype=float)
    q_values = np.full(len(p_values), np.nan, dtype=float)
    valid_positions = np.flatnonzero(np.isfinite(p_values))
    if not len(valid_positions):
        return q_values
    valid = p_values[valid_positions]
    order = np.argsort(valid, kind="mergesort")
    ranked = valid[order]
    adjusted = ranked * len(ranked) / np.arange(1, len(ranked) + 1)
    adjusted = np.minimum.accumulate(adjusted[::-1])[::-1]
    adjusted = np.clip(adjusted, 0.0, 1.0)
    restored = np.empty_like(adjusted)
    restored[order] = adjusted
    q_values[valid_positions] = restored
    return q_values


def _bootstrap_seed(base_seed: int, candidate_id: str) -> int:
    digest = hashlib.sha256(candidate_id.encode("utf-8")).digest()
    return (base_seed + int.from_bytes(digest[:4], "big")) % (2**32)


def moving_block_mean_interval(
    values: np.ndarray,
    *,
    block_size: int,
    iterations: int,
    seed: int,
) -> tuple[float, float]:
    """Bootstrap a mean with circular moving blocks."""
    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if not len(array):
        return float("nan"), float("nan")
    effective_block = min(max(int(block_size), 1), len(array))
    circular = np.concatenate([array, array[: effective_block - 1]])
    cumulative = np.concatenate([[0.0], np.cumsum(circular)])
    block_means = (
        cumulative[effective_block:] - cumulative[:-effective_block]
    ) / effective_block
    blocks_per_sample = int(np.ceil(len(array) / effective_block))
    rng = np.random.default_rng(seed)
    draws = rng.integers(
        0,
        len(block_means),
        size=(iterations, blocks_per_sample),
    )
    means = block_means[draws].mean(axis=1)
    lower, upper = np.quantile(means, [0.025, 0.975])
    return float(lower), float(upper)


def _leave_one_era_out_minimum(
    dates: pd.DatetimeIndex,
    values: np.ndarray,
) -> tuple[float, int]:
    eras = (dates.year // 5) * 5
    unique = np.unique(eras)
    if len(unique) < 2:
        return float("nan"), len(unique)
    means = [
        float(np.mean(values[eras != era]))
        for era in unique
        if np.any(eras != era)
    ]
    return min(means), len(unique)


def _discover_direction(x_train: np.ndarray, y_train: np.ndarray) -> int:
    positive_mean = float(np.mean(x_train[y_train == 1]))
    negative_mean = float(np.mean(x_train[y_train == 0]))
    return 1 if positive_mean >= negative_mean else -1


def _build_candidate_frame(
    candidate: ScreeningCandidate,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    development_end: pd.Timestamp,
) -> pd.DataFrame:
    label_column = f"{candidate.side}_label_{candidate.horizon}d"
    required = {
        label_column,
        f"forward_return_{candidate.horizon}d",
        f"future_mfe_{candidate.horizon}d",
        f"future_mae_{candidate.horizon}d",
        f"{candidate.side}_touch_day_{candidate.horizon}d",
    }
    missing = required.difference(labels.columns)
    if missing:
        raise DataContractError(
            f"Labels are missing columns for {candidate.candidate_id}: "
            + ", ".join(sorted(missing))
        )
    frame = pd.DataFrame(
        {
            "x": pd.to_numeric(
                features[candidate.feature_name], errors="coerce"
            ),
            "y": pd.to_numeric(labels[label_column], errors="coerce"),
            "forward_return": pd.to_numeric(
                labels[f"forward_return_{candidate.horizon}d"],
                errors="coerce",
            ),
            "future_mfe": pd.to_numeric(
                labels[f"future_mfe_{candidate.horizon}d"],
                errors="coerce",
            ),
            "future_mae": pd.to_numeric(
                labels[f"future_mae_{candidate.horizon}d"],
                errors="coerce",
            ),
            "touch_day": pd.to_numeric(
                labels[f"{candidate.side}_touch_day_{candidate.horizon}d"],
                errors="coerce",
            ),
        },
        index=features.index,
    )
    frame = frame.loc[frame.index <= development_end]
    frame = frame.loc[frame["x"].notna() & frame["y"].notna()].copy()
    if frame.empty:
        return frame
    frame["y"] = frame["y"].astype(int)
    if not frame["y"].isin([0, 1]).all():
        raise DataContractError(
            f"Label {label_column} contains values outside 0/1"
        )
    numeric = frame.select_dtypes(include=[np.number]).to_numpy(dtype=float)
    if np.isinf(numeric).any():
        raise DataContractError(
            f"Candidate frame contains infinity: {candidate.candidate_id}"
        )
    return frame


def _feature_coverage(
    candidate: ScreeningCandidate,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    development_end: pd.Timestamp,
) -> float:
    label_column = f"{candidate.side}_label_{candidate.horizon}d"
    x = features[candidate.feature_name].loc[:development_end]
    first_valid = x.first_valid_index()
    if first_valid is None:
        return 0.0
    eligible = labels[label_column].loc[first_valid:development_end].notna()
    denominator = int(eligible.sum())
    if denominator == 0:
        return 0.0
    numerator = int((eligible & x.loc[first_valid:development_end].notna()).sum())
    return numerator / denominator


def _evaluate_candidate(
    candidate: ScreeningCandidate,
    *,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    windows: Sequence[tuple[str, pd.Timestamp, pd.Timestamp]],
    development_end: pd.Timestamp,
    settings: ScreeningSettings,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    int | None,
    str,
]:
    frame = _build_candidate_frame(
        candidate,
        features,
        labels,
        development_end=development_end,
    )
    coverage = _feature_coverage(
        candidate,
        features,
        labels,
        development_end=development_end,
    )
    expected_direction = candidate.expected_direction
    direction_source = (
        "frozen_economic_hypothesis"
        if expected_direction is not None
        else "unresolved"
    )
    fold_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    published_predictions: list[dict[str, Any]] = []
    all_predictions: list[pd.DataFrame] = []

    if not frame.empty:
        for fold_id, validation_start, validation_end in windows:
            train_end = _purged_train_end(
                sessions,
                validation_start,
                settings.embargo_sessions,
            )
            if train_end is None:
                continue
            train = frame.loc[frame.index <= train_end]
            validation = frame.loc[
                (frame.index >= validation_start)
                & (frame.index <= validation_end)
            ]
            if train.empty or validation.empty:
                continue
            minimum_start = train.index.max() - pd.DateOffset(
                years=settings.minimum_train_years
            )
            if train.index.min() > minimum_start:
                continue
            train_positive = int(train["y"].sum())
            validation_positive = int(validation["y"].sum())
            if (
                len(train) < settings.minimum_train_rows
                or len(validation) < settings.minimum_validation_rows
                or train_positive < settings.minimum_train_positives
                or len(train) - train_positive
                < settings.minimum_train_positives
            ):
                continue

            x_train_raw = train["x"].to_numpy(dtype=float)
            y_train = train["y"].to_numpy(dtype=int)
            x_validation_raw = validation["x"].to_numpy(dtype=float)
            y_validation = validation["y"].to_numpy(dtype=int)
            if expected_direction is None:
                expected_direction = _discover_direction(x_train_raw, y_train)
                direction_source = f"first_training_fold:{fold_id}"

            lower, upper = np.quantile(
                x_train_raw,
                [
                    settings.winsor_lower_quantile,
                    settings.winsor_upper_quantile,
                ],
            )
            clipped_train = np.clip(x_train_raw, lower, upper)
            clipped_validation = np.clip(x_validation_raw, lower, upper)
            center = float(clipped_train.mean())
            scale = float(clipped_train.std(ddof=0))
            if not np.isfinite(scale) or scale <= 1e-12:
                continue
            z_train = (clipped_train - center) / scale
            z_validation = (clipped_validation - center) / scale
            intercept, coefficient = _fit_logistic(
                z_train,
                y_train,
                ridge_penalty=settings.ridge_penalty,
            )
            probability = expit(intercept + coefficient * z_validation)
            baseline = np.full(
                len(validation),
                float(y_train.mean()),
                dtype=float,
            )
            oriented_train = expected_direction * clipped_train
            oriented_validation = expected_direction * clipped_validation
            signal_threshold = float(
                np.quantile(oriented_train, settings.signal_quantile)
            )
            signal = oriented_validation >= signal_threshold
            metrics = _classification_metrics(
                y_validation,
                probability,
                baseline,
                calibration_bins=settings.calibration_bins,
            )
            signal_precision = (
                float(y_validation[signal].mean())
                if signal.any()
                else float("nan")
            )
            signal_recall = (
                float(signal[y_validation == 1].mean())
                if validation_positive
                else float("nan")
            )
            validation_direction_auc = _roc_auc(
                y_validation,
                expected_direction * x_validation_raw,
            )
            validation_direction_match: bool | None = (
                validation_direction_auc > 0.5
                if np.isfinite(validation_direction_auc)
                else None
            )
            fold_rows.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "feature_name": candidate.feature_name,
                    "side": candidate.side,
                    "horizon": candidate.horizon,
                    "fold_id": fold_id,
                    "train_start": train.index.min(),
                    "train_end": train.index.max(),
                    "validation_start": validation.index.min(),
                    "validation_end": validation.index.max(),
                    "embargo_sessions": settings.embargo_sessions,
                    "train_rows": len(train),
                    "train_positives": train_positive,
                    "validation_rows": len(validation),
                    "validation_positives": validation_positive,
                    "validation_class_informative": (
                        validation_positive
                        >= settings.minimum_validation_positives
                        and len(validation) - validation_positive
                        >= settings.minimum_validation_positives
                    ),
                    "winsor_lower": float(lower),
                    "winsor_upper": float(upper),
                    "center": center,
                    "scale": scale,
                    "intercept": intercept,
                    "coefficient": coefficient,
                    "expected_direction": _direction_name(expected_direction),
                    "train_direction_match": (
                        coefficient * expected_direction > 0
                    ),
                    "validation_direction_auc": validation_direction_auc,
                    "direction_match": validation_direction_match,
                    "signal_threshold_oriented": signal_threshold,
                    "signal_rows": int(signal.sum()),
                    "signal_precision": signal_precision,
                    "signal_recall": signal_recall,
                    **metrics,
                }
            )

            quantile_edges = np.quantile(
                oriented_train,
                [0.20, 0.40, 0.60, 0.80],
            )
            quantile_bins = (
                np.searchsorted(
                    quantile_edges,
                    oriented_validation,
                    side="right",
                )
                + 1
            )
            for bin_number in range(1, 6):
                mask = quantile_bins == bin_number
                subset = validation.iloc[np.flatnonzero(mask)]
                event_rows.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "feature_name": candidate.feature_name,
                        "side": candidate.side,
                        "horizon": candidate.horizon,
                        "fold_id": fold_id,
                        "oriented_quantile_bin": bin_number,
                        "rows": int(mask.sum()),
                        "positives": int(y_validation[mask].sum()),
                        "event_rate": (
                            float(y_validation[mask].mean())
                            if mask.any()
                            else float("nan")
                        ),
                        "forward_return_mean": (
                            float(subset["forward_return"].mean())
                            if not subset.empty
                            else float("nan")
                        ),
                        "future_mfe_mean": (
                            float(subset["future_mfe"].mean())
                            if not subset.empty
                            else float("nan")
                        ),
                        "future_mae_mean": (
                            float(subset["future_mae"].mean())
                            if not subset.empty
                            else float("nan")
                        ),
                    }
                )

            prediction_frame = pd.DataFrame(
                {
                    "candidate_id": candidate.candidate_id,
                    "feature_name": candidate.feature_name,
                    "side": candidate.side,
                    "horizon": candidate.horizon,
                    "fold_id": fold_id,
                    "date": validation.index,
                    "feature_value": x_validation_raw,
                    "actual": y_validation,
                    "model_probability": probability,
                    "baseline_probability": baseline,
                    "signal": signal,
                    "touch_day": validation["touch_day"].to_numpy(dtype=float),
                }
            )
            all_predictions.append(prediction_frame)
            if candidate.is_confirmatory:
                published_predictions.extend(
                    prediction_frame.to_dict(orient="records")
                )

    if all_predictions:
        predictions = pd.concat(all_predictions, ignore_index=True)
        prediction_dates = pd.DatetimeIndex(predictions["date"])
        y_oos = predictions["actual"].to_numpy(dtype=int)
        probability_oos = predictions["model_probability"].to_numpy(dtype=float)
        baseline_oos = predictions["baseline_probability"].to_numpy(dtype=float)
        signal_oos = predictions["signal"].to_numpy(dtype=bool)
        aggregate_metrics = _classification_metrics(
            y_oos,
            probability_oos,
            baseline_oos,
            calibration_bins=settings.calibration_bins,
        )
        episode_metrics = _episode_metrics(
            prediction_dates,
            y_oos,
            signal_oos,
            sessions=sessions,
            horizon=candidate.horizon,
        )
        brier_delta = (y_oos - baseline_oos) ** 2 - (
            y_oos - probability_oos
        ) ** 2
        bootstrap_lower, bootstrap_upper = moving_block_mean_interval(
            brier_delta,
            block_size=settings.bootstrap_block_rows,
            iterations=settings.bootstrap_iterations,
            seed=_bootstrap_seed(settings.random_seed, candidate.candidate_id),
        )
        leave_one_era_min, oos_eras = _leave_one_era_out_minimum(
            prediction_dates,
            brier_delta,
        )
        touch_days = predictions["touch_day"].to_numpy(dtype=float)
        positive_touch_days = touch_days[y_oos == 1]
        signaled_touch_days = touch_days[(y_oos == 1) & signal_oos]
        median_touch_day = (
            float(np.nanmedian(positive_touch_days))
            if np.isfinite(positive_touch_days).any()
            else float("nan")
        )
        median_signaled_touch_day = (
            float(np.nanmedian(signaled_touch_days))
            if np.isfinite(signaled_touch_days).any()
            else float("nan")
        )
    else:
        predictions = pd.DataFrame()
        aggregate_metrics = {
            "prevalence": float("nan"),
            "average_precision": float("nan"),
            "pr_auc_lift": float("nan"),
            "roc_auc": float("nan"),
            "brier": float("nan"),
            "baseline_brier": float("nan"),
            "brier_skill": float("nan"),
            "calibration_error": float("nan"),
        }
        episode_metrics = {
            "positive_event_episodes": 0,
            "signal_episodes": 0,
            "event_precision": float("nan"),
            "event_recall": float("nan"),
            "false_alarm_episodes_per_year": float("nan"),
        }
        bootstrap_lower = float("nan")
        bootstrap_upper = float("nan")
        leave_one_era_min = float("nan")
        oos_eras = 0
        median_touch_day = float("nan")
        median_signaled_touch_day = float("nan")

    if expected_direction is None and not frame.empty:
        # No eligible walk-forward fold means the exploratory direction remains
        # unresolved; using the full sample here would leak future information.
        rank_metrics = _rank_test(
            frame,
            expected_direction=None,
            confirmatory=False,
            sessions=sessions,
            horizon=candidate.horizon,
        )
    else:
        rank_metrics = _rank_test(
            frame,
            expected_direction=expected_direction,
            confirmatory=candidate.is_confirmatory,
            sessions=sessions,
            horizon=candidate.horizon,
        ) if not frame.empty else {
            "rank_test_p_value": float("nan"),
            "rank_effect_auc": float("nan"),
            "rank_test_positive_rows": 0,
            "rank_test_negative_rows": 0,
        }

    if not frame.empty:
        episode_starts = _episode_starts(
            pd.DatetimeIndex(frame.index),
            frame["y"].to_numpy(dtype=int),
            sessions=sessions,
            horizon=candidate.horizon,
        )
        regime_eras = len(set((episode_starts.year // 5) * 5))
    else:
        episode_starts = pd.DatetimeIndex([])
        regime_eras = 0
    direction_matches = [
        bool(row["direction_match"])
        for row in fold_rows
        if row["direction_match"] is not None
    ]
    informative_folds = sum(
        bool(row["validation_class_informative"]) for row in fold_rows
    )
    direction_consistency = (
        float(np.mean(direction_matches))
        if direction_matches
        else float("nan")
    )
    if event_rows:
        event_frame = pd.DataFrame(event_rows)
        event_bins = (
            event_frame.groupby("oriented_quantile_bin", sort=True)[
                ["rows", "positives"]
            ]
            .sum()
            .reset_index()
        )
        event_bins = event_bins.loc[event_bins["rows"] > 0].copy()
        event_bins["event_rate"] = (
            event_bins["positives"] / event_bins["rows"]
        )
        if (
            len(event_bins) >= 3
            and event_bins["event_rate"].nunique() > 1
        ):
            quantile_monotonicity = float(
                np.corrcoef(
                    rankdata(event_bins["oriented_quantile_bin"]),
                    rankdata(event_bins["event_rate"]),
                )[0, 1]
            )
        else:
            quantile_monotonicity = float("nan")
        bin_rates = event_bins.set_index("oriented_quantile_bin")[
            "event_rate"
        ]
        extreme_bin_lift = (
            float(bin_rates.loc[5] - bin_rates.loc[1])
            if 1 in bin_rates.index and 5 in bin_rates.index
            else float("nan")
        )
    else:
        quantile_monotonicity = float("nan")
        extreme_bin_lift = float("nan")
    score = {
        "candidate_id": candidate.candidate_id,
        "feature_name": candidate.feature_name,
        "side": candidate.side,
        "horizon": candidate.horizon,
        "family": candidate.family,
        "hypothesis_tier": candidate.hypothesis_tier,
        "expected_direction": _direction_name(expected_direction),
        "direction_source": direction_source,
        "feature_coverage": coverage,
        "development_rows": len(frame),
        "development_positives": int(frame["y"].sum()) if not frame.empty else 0,
        "development_event_episodes": len(episode_starts),
        "development_regime_eras": regime_eras,
        "walk_forward_folds": len(fold_rows),
        "class_informative_folds": informative_folds,
        "direction_evaluable_folds": len(direction_matches),
        "direction_consistency": direction_consistency,
        "quantile_monotonicity": quantile_monotonicity,
        "extreme_bin_event_rate_lift": extreme_bin_lift,
        "oos_rows": len(predictions),
        "oos_eras": oos_eras,
        **rank_metrics,
        **aggregate_metrics,
        **episode_metrics,
        "brier_delta_bootstrap_lower_95": bootstrap_lower,
        "brier_delta_bootstrap_upper_95": bootstrap_upper,
        "leave_one_era_out_min_brier_delta": leave_one_era_min,
        "median_positive_touch_day": median_touch_day,
        "median_signaled_positive_touch_day": median_signaled_touch_day,
    }
    return (
        score,
        fold_rows,
        event_rows,
        published_predictions,
        expected_direction,
        direction_source,
    )


def _apply_scorecard_gates(
    scorecard: pd.DataFrame,
    settings: ScreeningSettings,
) -> pd.DataFrame:
    result = scorecard.copy()
    result["g1_data_quality"] = (
        result["feature_coverage"] >= settings.minimum_feature_coverage
    )
    result["g2_sample_depth"] = (
        (result["development_event_episodes"] >= settings.minimum_event_episodes)
        & (result["development_regime_eras"] >= settings.minimum_regime_eras)
        & (result["class_informative_folds"] >= settings.minimum_fold_count)
    )
    result["g3_direction"] = (
        result["direction_consistency"] >= settings.direction_consistency
    )
    result["g4_oos_probability"] = (
        (result["brier_skill"] > 0)
        & (result["average_precision"] > result["prevalence"])
    )
    result["g5_dependence_robustness"] = (
        (result["brier_delta_bootstrap_lower_95"] > 0)
        & (result["leave_one_era_out_min_brier_delta"] > 0)
    )
    result["g6_multiple_testing"] = result["fdr_q_value"] <= settings.fdr_q
    gate_columns = [
        "g1_data_quality",
        "g2_sample_depth",
        "g3_direction",
        "g4_oos_probability",
        "g5_dependence_robustness",
        "g6_multiple_testing",
    ]
    result["stage_1_numeric_pass"] = result[gate_columns].all(axis=1)
    confirmatory = result["hypothesis_tier"] == "confirmatory"
    insufficient = (
        ~result["g1_data_quality"]
        | ~result["g2_sample_depth"]
    )
    result["screening_status"] = "EXPLORATORY_ONLY"
    result.loc[confirmatory & insufficient, "screening_status"] = (
        "INSUFFICIENT_EVIDENCE"
    )
    result.loc[
        confirmatory & ~insufficient & ~result["stage_1_numeric_pass"],
        "screening_status",
    ] = "STAGE_1_FAIL"
    result.loc[
        confirmatory & result["stage_1_numeric_pass"],
        "screening_status",
    ] = "STAGE_1_PASS"
    result["production_approved"] = False
    result["pending_final_gates"] = "G7_PARAMETER,G8_INCREMENTAL,G9_ECONOMIC,G10_SHADOW"
    return result


def run_univariate_screening(
    *,
    features: pd.DataFrame,
    labels: pd.DataFrame,
    feature_registry: pd.DataFrame,
    candidates: Sequence[ScreeningCandidate],
    settings: ScreeningSettings,
    registry_metadata: Mapping[str, Any] | None = None,
) -> ScreeningOutputs:
    """Evaluate every candidate without reading or scoring the sealed holdout."""
    if features.empty or labels.empty:
        raise DataContractError("Features and labels must be non-empty")
    if not features.index.equals(labels.index):
        raise DataContractError("Features and labels must share the exact index")
    if (
        "feature_name" not in feature_registry.columns
        or feature_registry["feature_name"].duplicated().any()
        or feature_registry["feature_name"].tolist()
        != features.columns.tolist()
    ):
        raise DataContractError(
            "Feature registry must uniquely match the feature matrix order"
        )
    sessions = _validate_sessions(pd.DatetimeIndex(features.index))
    if not features.index.equals(sessions):
        features = features.copy()
        labels = labels.copy()
        features.index = sessions
        labels.index = sessions
    missing_features = {
        candidate.feature_name for candidate in candidates
    }.difference(features.columns)
    if missing_features:
        raise DataContractError(
            "Candidates reference missing feature columns: "
            + ", ".join(sorted(missing_features))
        )
    windows, development_end = validation_windows(sessions, settings)

    score_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    event_rows: list[dict[str, Any]] = []
    prediction_rows: list[dict[str, Any]] = []
    registry_rows: list[dict[str, Any]] = []
    for candidate in candidates:
        (
            score,
            candidate_folds,
            candidate_events,
            candidate_predictions,
            resolved_direction,
            direction_source,
        ) = _evaluate_candidate(
            candidate,
            features=features,
            labels=labels,
            sessions=sessions,
            windows=windows,
            development_end=development_end,
            settings=settings,
        )
        score_rows.append(score)
        fold_rows.extend(candidate_folds)
        event_rows.extend(candidate_events)
        prediction_rows.extend(candidate_predictions)
        registry_row = candidate.as_dict()
        registry_row["resolved_expected_direction"] = _direction_name(
            resolved_direction
        )
        registry_row["direction_source"] = direction_source
        registry_rows.append(registry_row)

    scorecard = pd.DataFrame(score_rows)
    scorecard["fdr_family"] = (
        scorecard["side"]
        + "_"
        + scorecard["horizon"].astype(str)
        + "d"
    )
    scorecard["fdr_q_value"] = np.nan
    for _, positions in scorecard.groupby("fdr_family").groups.items():
        position_list = list(positions)
        scorecard.loc[position_list, "fdr_q_value"] = benjamini_hochberg(
            scorecard.loc[position_list, "rank_test_p_value"]
        )
    scorecard = _apply_scorecard_gates(scorecard, settings)
    scorecard = scorecard.sort_values(
        [
            "hypothesis_tier",
            "screening_status",
            "side",
            "horizon",
            "fdr_q_value",
            "brier_skill",
        ],
        ascending=[True, True, True, True, True, False],
        kind="stable",
    ).reset_index(drop=True)

    candidate_registry_frame = pd.DataFrame(registry_rows)
    event_studies = pd.DataFrame(event_rows)
    fold_results = pd.DataFrame(fold_rows)
    predictions = pd.DataFrame(prediction_rows)
    status_counts = {
        str(key): int(value)
        for key, value in scorecard["screening_status"].value_counts().items()
    }
    stage_pass = scorecard.loc[
        scorecard["screening_status"] == "STAGE_1_PASS",
        ["candidate_id", "feature_name", "side", "horizon"],
    ].to_dict(orient="records")
    exploratory_numeric_pass = int(
        (
            (scorecard["hypothesis_tier"] == "exploratory")
            & scorecard["stage_1_numeric_pass"]
        ).sum()
    )
    summary = {
        "status": "SUCCESS",
        "holdout_start": settings.holdout_start,
        "holdout_status": "SEALED_NOT_EVALUATED",
        "development_end_after_embargo": development_end.date().isoformat(),
        "embargo_sessions": settings.embargo_sessions,
        "validation_windows": len(windows),
        "candidate_tests": len(scorecard),
        "confirmatory_tests": int(
            (scorecard["hypothesis_tier"] == "confirmatory").sum()
        ),
        "exploratory_tests": int(
            (scorecard["hypothesis_tier"] == "exploratory").sum()
        ),
        "status_counts": status_counts,
        "stage_1_pass_count": len(stage_pass),
        "stage_1_pass_candidates": stage_pass,
        "exploratory_numeric_pass_count": exploratory_numeric_pass,
        "production_approved_count": 0,
        "remaining_gates": [
            "G7 parameter perturbation",
            "G8 incremental information",
            "G9 economic value",
            "G10 shadow operation",
        ],
        "registry": dict(registry_metadata or {}),
    }
    return ScreeningOutputs(
        candidate_registry=candidate_registry_frame,
        event_studies=event_studies,
        fold_results=fold_results,
        predictions=predictions,
        scorecard=scorecard,
        summary=summary,
    )


__all__ = [
    "ScreeningCandidate",
    "ScreeningOutputs",
    "benjamini_hochberg",
    "load_candidate_registry",
    "moving_block_mean_interval",
    "run_univariate_screening",
    "validation_windows",
]
