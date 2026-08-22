from __future__ import annotations

from pathlib import Path

import pytest

from src.research_universes.models import (
    FactorPublicationMode,
    ResearchUniverseRole,
    UniversePurpose,
)
from src.research_universes.registry import (
    ResearchUniverseRegistryError,
    load_research_universe_registry,
)


def test_production_registry_separates_primary_secondary_and_reference():
    registry = load_research_universe_registry()

    assert registry.ids() == [
        "US_EQUITY_COVERAGE",
        "US_LIQUID_5M",
        "SP500",
        "NASDAQ100",
        "MAG7",
    ]
    assert registry.get("US_EQUITY_COVERAGE").purpose == UniversePurpose.COVERAGE
    assert (
        registry.get("US_EQUITY_COVERAGE").factor_publication_mode
        == FactorPublicationMode.RAW_ONLY
    )
    assert registry.get("US_LIQUID_5M").purpose == UniversePurpose.ESTIMATION
    assert (
        registry.get("US_LIQUID_5M").factor_publication_mode
        == FactorPublicationMode.FACTOR_DATA
    )
    assert registry.get("US_LIQUID_5M").parent_data_universe == "US_EQUITY_COVERAGE"
    assert registry.get("SP500").role == ResearchUniverseRole.PRIMARY
    assert registry.get("NASDAQ100").role == ResearchUniverseRole.SECONDARY
    assert registry.get("MAG7").role == ResearchUniverseRole.REFERENCE
    assert [entry.universe_id for entry in registry.cross_universe_entries()] == [
        "SP500",
        "NASDAQ100",
    ]
    assert [entry.universe_id for entry in registry.full_research_entries()] == [
        "SP500",
        "NASDAQ100",
        "MAG7",
    ]
    assert [entry.universe_id for entry in registry.factor_data_entries()] == [
        "US_LIQUID_5M",
        "SP500",
        "NASDAQ100",
        "MAG7",
    ]
    assert [entry.universe_id for entry in registry.embedded_pit_entries()] == [
        "SP500",
        "NASDAQ100",
    ]
    assert not registry.get("MAG7").confidence_enabled

    from scripts.run_factor_research import _configured_universes as research_jobs
    from scripts.run_data_pipeline import _configured_universes as data_jobs
    from scripts.run_mvp import _enabled_universes as legacy_research_jobs

    expected_full_research = ["SP500", "NASDAQ100", "MAG7"]
    assert research_jobs() == expected_full_research
    assert data_jobs() == expected_full_research
    assert legacy_research_jobs() == expected_full_research


def test_reference_universe_cannot_enter_overall_verdict(tmp_path: Path):
    registry = tmp_path / "research.yaml"
    registry.write_text(
        """
schema_version: 1
universes:
  SP500:
    role: PRIMARY
    membership_type: PIT
    benchmark: SPY
    confidence_enabled: true
    cross_universe_enabled: true
    minimum_cross_section: 100
  MAG7:
    role: REFERENCE
    membership_type: STATIC
    benchmark: QQQ
    confidence_enabled: false
    cross_universe_enabled: true
    minimum_cross_section: 3
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ResearchUniverseRegistryError, match="REFERENCE"):
        load_research_universe_registry(registry)
