"""Typed domain model separating statistical research from target universes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class ResearchUniverseRole(StrEnum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    REFERENCE = "REFERENCE"


class MembershipType(StrEnum):
    PIT = "PIT"
    STATIC = "STATIC"


@dataclass(frozen=True)
class ResearchUniverse:
    universe_id: str
    role: ResearchUniverseRole
    membership_type: MembershipType
    benchmark: str
    confidence_enabled: bool
    cross_universe_enabled: bool
    minimum_cross_section: int
    minimum_industry_coverage: float

    @property
    def contributes_to_overall_verdict(self) -> bool:
        return self.role in {
            ResearchUniverseRole.PRIMARY,
            ResearchUniverseRole.SECONDARY,
        }

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["role"] = self.role.value
        payload["membership_type"] = self.membership_type.value
        payload["contributes_to_overall_verdict"] = (
            self.contributes_to_overall_verdict
        )
        return payload


__all__ = ["MembershipType", "ResearchUniverse", "ResearchUniverseRole"]
