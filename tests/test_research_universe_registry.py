from __future__ import annotations

from pathlib import Path

import pytest

from src.research_universes.models import ResearchUniverseRole
from src.research_universes.registry import (
    ResearchUniverseRegistryError,
    load_research_universe_registry,
)


def test_production_registry_separates_primary_secondary_and_reference():
    registry = load_research_universe_registry()

    assert registry.ids() == ["SP500", "NASDAQ100", "MAG7"]
    assert registry.get("SP500").role == ResearchUniverseRole.PRIMARY
    assert registry.get("NASDAQ100").role == ResearchUniverseRole.SECONDARY
    assert registry.get("MAG7").role == ResearchUniverseRole.REFERENCE
    assert [entry.universe_id for entry in registry.cross_universe_entries()] == [
        "SP500",
        "NASDAQ100",
    ]
    assert not registry.get("MAG7").confidence_enabled


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
