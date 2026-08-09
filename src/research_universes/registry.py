"""Strict loader for the version-controlled research-universe registry."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.config import CONFIG, PROJECT_ROOT
from src.research_universes.models import (
    MembershipType,
    ResearchUniverse,
    ResearchUniverseRole,
)
from src.utils.identifiers import canonical_ticker, safe_path_component


class ResearchUniverseRegistryError(ValueError):
    """Registry structure or role semantics are invalid."""


class ResearchUniverseRegistry:
    def __init__(self, entries: dict[str, ResearchUniverse], *, source: Path):
        self._entries = entries
        self.source = source

    def get(self, universe_id: str) -> ResearchUniverse:
        key = safe_path_component(universe_id.upper(), label="research_universe")
        try:
            return self._entries[key]
        except KeyError as exc:
            raise ResearchUniverseRegistryError(
                f"Unknown research universe: {key}"
            ) from exc

    def list(self) -> tuple[ResearchUniverse, ...]:
        return tuple(self._entries.values())

    def ids(self) -> list[str]:
        return list(self._entries)

    def confidence_universes(self) -> tuple[ResearchUniverse, ...]:
        return tuple(entry for entry in self.list() if entry.confidence_enabled)

    def cross_universe_entries(self) -> tuple[ResearchUniverse, ...]:
        return tuple(entry for entry in self.list() if entry.cross_universe_enabled)

    def primary(self) -> ResearchUniverse:
        """Return the single PRIMARY universe guaranteed by registry validation."""
        return next(
            entry
            for entry in self.list()
            if entry.role == ResearchUniverseRole.PRIMARY
        )


def _entry(universe_id: str, payload: Any) -> ResearchUniverse:
    if not isinstance(payload, dict):
        raise ResearchUniverseRegistryError(f"{universe_id} must be an object")
    try:
        entry = ResearchUniverse(
            universe_id=safe_path_component(
                universe_id.upper(), label="research_universe"
            ),
            role=ResearchUniverseRole(str(payload["role"]).upper()),
            membership_type=MembershipType(
                str(payload["membership_type"]).upper()
            ),
            benchmark=canonical_ticker(payload["benchmark"], label="benchmark"),
            confidence_enabled=bool(payload["confidence_enabled"]),
            cross_universe_enabled=bool(payload["cross_universe_enabled"]),
            minimum_cross_section=int(payload["minimum_cross_section"]),
            minimum_industry_coverage=float(
                payload.get("minimum_industry_coverage", 0.0)
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchUniverseRegistryError(
            f"Invalid research universe {universe_id}: {exc}"
        ) from exc
    if entry.minimum_cross_section < 3:
        raise ResearchUniverseRegistryError(
            f"{universe_id}.minimum_cross_section must be at least 3"
        )
    if not 0.0 <= entry.minimum_industry_coverage <= 1.0:
        raise ResearchUniverseRegistryError(
            f"{universe_id}.minimum_industry_coverage must be between 0 and 1"
        )
    if entry.role == ResearchUniverseRole.REFERENCE and entry.cross_universe_enabled:
        raise ResearchUniverseRegistryError(
            f"REFERENCE universe {universe_id} cannot enter the overall verdict"
        )
    return entry


def load_research_universe_registry(
    path: str | Path | None = None,
) -> ResearchUniverseRegistry:
    configured = path or getattr(
        CONFIG.research_universes,
        "registry_path",
        "configs/research_universes.yaml",
    )
    source = Path(configured)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise ResearchUniverseRegistryError(
            "Unsupported research-universe registry schema"
        )
    raw_entries = payload.get("universes")
    if not isinstance(raw_entries, dict) or not raw_entries:
        raise ResearchUniverseRegistryError("Registry must contain universes")
    entries = {str(key).upper(): _entry(str(key), value) for key, value in raw_entries.items()}
    primary = [entry for entry in entries.values() if entry.role == ResearchUniverseRole.PRIMARY]
    if len(primary) != 1:
        raise ResearchUniverseRegistryError("Registry requires exactly one PRIMARY")
    return ResearchUniverseRegistry(entries, source=source)


@lru_cache(maxsize=1)
def research_universe_registry() -> ResearchUniverseRegistry:
    return load_research_universe_registry()


__all__ = [
    "ResearchUniverseRegistry",
    "ResearchUniverseRegistryError",
    "load_research_universe_registry",
    "research_universe_registry",
]
