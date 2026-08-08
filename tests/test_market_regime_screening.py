from __future__ import annotations

from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest
from scipy.special import expit

from src.market_regime_research.artifacts import publish_research_run
from src.market_regime_research.models import (
    DataContractError,
    FeatureBundle,
    FeatureDefinition,
    ScreeningRunResult,
)
from src.market_regime_research.screening import (
    ScreeningCandidate,
    benjamini_hochberg,
    load_candidate_registry,
    run_univariate_screening,
    validation_windows,
)
from src.market_regime_research.screening_artifacts import (
    publish_screening_run,
)
from src.market_regime_research.screening_pipeline import (
    load_validated_research_run,
)
from src.market_regime_research.settings import ScreeningSettings


def _screening_fixture():
    index = pd.bdate_range("2000-01-03", "2014-12-31", name="date")
    rng = np.random.default_rng(90210)
    feature = rng.normal(size=len(index))
    probability = expit(-1.6 + 2.4 * feature)
    outcome = rng.binomial(1, probability)
    features = pd.DataFrame({"predictive": feature}, index=index)
    labels = pd.DataFrame(
        {
            "top_label_5d": pd.Series(outcome, index=index, dtype="Int8"),
            "forward_return_5d": rng.normal(0, 0.02, len(index)),
            "future_mfe_5d": rng.uniform(0, 0.04, len(index)),
            "future_mae_5d": -rng.uniform(0, 0.04, len(index)),
            "top_touch_day_5d": np.where(
                outcome == 1,
                rng.integers(1, 6, len(index)),
                np.nan,
            ),
        },
        index=index,
    )
    registry = pd.DataFrame(
        {
            "feature_name": ["predictive"],
            "group": ["fixture"],
        }
    )
    candidate = ScreeningCandidate(
        candidate_id="predictive_top__5d",
        feature_name="predictive",
        side="top",
        horizon=5,
        expected_direction=1,
        family="fixture",
        mechanism="Synthetic positive relationship.",
        overlap_reason="Fixture.",
        hypothesis_tier="confirmatory",
        registration_source="fixture",
    )
    settings = ScreeningSettings(
        holdout_start="2013-01-01",
        first_validation_start="2005-01-01",
        validation_years=2,
        minimum_train_years=2,
        embargo_sessions=20,
        minimum_train_rows=252,
        minimum_validation_rows=100,
        minimum_train_positives=10,
        minimum_validation_positives=2,
        minimum_fold_count=3,
        minimum_event_episodes=20,
        minimum_regime_eras=2,
        minimum_feature_coverage=0.95,
        bootstrap_iterations=200,
        bootstrap_block_rows=20,
        random_seed=11,
        scan_unregistered=False,
    )
    return features, labels, registry, [candidate], settings


def test_benjamini_hochberg_preserves_order_and_nan():
    observed = benjamini_hochberg([0.01, np.nan, 0.04, 0.03, 0.002])

    assert observed[0] == pytest.approx(0.02)
    assert np.isnan(observed[1])
    assert observed[2] == pytest.approx(0.04)
    assert observed[3] == pytest.approx(0.04)
    assert observed[4] == pytest.approx(0.008)


def test_validation_cutoff_purges_the_holdout_boundary():
    features, _, _, _, settings = _screening_fixture()

    windows, development_end = validation_windows(features.index, settings)
    holdout_position = features.index.searchsorted(
        pd.Timestamp(settings.holdout_start)
    )

    assert development_end == features.index[
        holdout_position - settings.embargo_sessions - 1
    ]
    assert windows[-1][2] == development_end


def test_holdout_mutation_cannot_change_screening_results():
    features, labels, registry, candidates, settings = _screening_fixture()
    original = run_univariate_screening(
        features=features,
        labels=labels,
        feature_registry=registry,
        candidates=candidates,
        settings=settings,
    )
    mutated_features = features.copy()
    mutated_labels = labels.copy()
    holdout = mutated_features.index >= pd.Timestamp(settings.holdout_start)
    mutated_features.loc[holdout, "predictive"] = 1_000_000
    mutated_labels.loc[holdout, "top_label_5d"] = (
        1 - mutated_labels.loc[holdout, "top_label_5d"].astype(int)
    )

    mutated = run_univariate_screening(
        features=mutated_features,
        labels=mutated_labels,
        feature_registry=registry,
        candidates=candidates,
        settings=settings,
    )

    pd.testing.assert_frame_equal(original.scorecard, mutated.scorecard)
    pd.testing.assert_frame_equal(original.fold_results, mutated.fold_results)
    assert original.predictions["date"].max() < pd.Timestamp(
        settings.holdout_start
    )
    assert (
        original.scorecard.loc[0, "screening_status"] == "STAGE_1_PASS"
    )
    assert np.isfinite(
        original.scorecard.loc[0, "quantile_monotonicity"]
    )
    assert np.isfinite(
        original.scorecard.loc[0, "median_signaled_positive_touch_day"]
    )


def test_every_fold_obeys_the_configured_embargo():
    features, labels, registry, candidates, settings = _screening_fixture()
    outputs = run_univariate_screening(
        features=features,
        labels=labels,
        feature_registry=registry,
        candidates=candidates,
        settings=settings,
    )

    for row in outputs.fold_results.itertuples(index=False):
        train_position = features.index.get_loc(row.train_end)
        validation_position = features.index.get_loc(row.validation_start)
        assert validation_position - train_position > settings.embargo_sessions
        assert row.direction_match == (row.validation_direction_auc > 0.5)


def test_candidate_registry_marks_unregistered_tests_exploratory(tmp_path):
    path = tmp_path / "candidates.yaml"
    path.write_text(
        """
registry_version: "test"
frozen_at: "2026-01-01"
registration_type: "fixture"
hypotheses:
  - candidate_id: alpha_top
    feature_name: alpha
    side: top
    horizons: [5]
    expected_direction: higher
    family: fixture
    mechanism: fixture mechanism
    overlap_reason: fixture overlap
""",
        encoding="utf-8",
    )
    feature_registry = pd.DataFrame(
        {
            "feature_name": ["alpha", "beta"],
            "group": ["one", "two"],
        }
    )

    candidates, metadata = load_candidate_registry(
        path,
        feature_registry,
        horizons=[5, 20],
        scan_unregistered=True,
    )

    assert len(candidates) == 8
    assert metadata["confirmatory_tests"] == 1
    assert metadata["exploratory_tests"] == 7
    assert sum(candidate.is_confirmatory for candidate in candidates) == 1


def test_candidate_registry_rejects_unknown_feature(tmp_path):
    path = tmp_path / "candidates.yaml"
    path.write_text(
        """
registry_version: "test"
hypotheses:
  - candidate_id: missing
    feature_name: absent
    side: top
    horizons: [5]
    expected_direction: higher
    family: fixture
    mechanism: fixture mechanism
    overlap_reason: fixture overlap
""",
        encoding="utf-8",
    )

    with pytest.raises(DataContractError, match="unknown feature"):
        load_candidate_registry(
            path,
            pd.DataFrame({"feature_name": ["alpha"], "group": ["fixture"]}),
            horizons=[5],
            scan_unregistered=False,
        )


def test_screening_artifacts_are_immutable_and_self_describing(tmp_path):
    features, labels, registry, candidates, settings = _screening_fixture()
    outputs = run_univariate_screening(
        features=features,
        labels=labels,
        feature_registry=registry,
        candidates=candidates,
        settings=settings,
    )

    result = publish_screening_run(
        output_root=tmp_path,
        outputs=outputs,
        settings=settings,
        source_manifest={"run_id": "stage_a_fixture"},
        screening_id="screen_fixture",
    )

    assert isinstance(result, ScreeningRunResult)
    assert result.scorecard_path.exists()
    assert "SEALED_NOT_EVALUATED" in result.report_path.read_text(
        encoding="utf-8"
    ) or "封存集" in result.report_path.read_text(encoding="utf-8")
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    pointer = json.loads(
        (tmp_path / "latest_screening.json").read_text(encoding="utf-8")
    )
    assert manifest["source"]["run_id"] == "stage_a_fixture"
    assert manifest["holdout_status"] == "SEALED_NOT_EVALUATED"
    assert pointer["screening_id"] == "screen_fixture"

    with pytest.raises(FileExistsError):
        publish_screening_run(
            output_root=tmp_path,
            outputs=outputs,
            settings=settings,
            source_manifest={"run_id": "stage_a_fixture"},
            screening_id="screen_fixture",
        )


def test_screening_refuses_tampered_stage_a_artifacts(tmp_path):
    index = pd.bdate_range("2025-01-02", periods=20, name="date")
    bundle = FeatureBundle(
        values=pd.DataFrame({"alpha": np.arange(20, dtype=float)}, index=index),
        registry=[
            FeatureDefinition(
                feature_name="alpha",
                group="fixture",
                instrument="fixture",
                formula="fixture",
                lookback_sessions=1,
                description="fixture",
            )
        ],
    )
    labels = pd.DataFrame({"top_label_5d": 0}, index=index)
    research = publish_research_run(
        output_root=tmp_path,
        features=bundle,
        labels=labels,
        input_manifest={"fixture": True},
        diagnostics={"fixture": True},
        run_id="stage_a_fixture",
    )
    load_validated_research_run(research.run_dir)
    tampered = pd.read_parquet(research.features_path)
    tampered.iloc[0, 0] = -999
    tampered.to_parquet(research.features_path)

    with pytest.raises(DataContractError, match="hash differs"):
        load_validated_research_run(research.run_dir)
