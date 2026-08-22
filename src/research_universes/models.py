"""Typed domain model separating statistical research from target universes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class ResearchUniverseRole(StrEnum):
    NONE = "NONE"
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    REFERENCE = "REFERENCE"


class MembershipType(StrEnum):
    PIT = "PIT"
    STATIC = "STATIC"


class UniversePurpose(StrEnum):
    COVERAGE = "COVERAGE"
    ESTIMATION = "ESTIMATION"
    VALIDATION = "VALIDATION"
    REFERENCE = "REFERENCE"


class FactorPublicationMode(StrEnum):
    NONE = "NONE"
    RAW_ONLY = "RAW_ONLY"
    FACTOR_DATA = "FACTOR_DATA"
    FULL_RESEARCH = "FULL_RESEARCH"


@dataclass(frozen=True)
class ResearchUniverse:
    universe_id: str
    display_name: str
    purpose: UniversePurpose
    role: ResearchUniverseRole
    membership_type: MembershipType
    factor_publication_mode: FactorPublicationMode
    benchmark: str | None
    confidence_enabled: bool
    cross_universe_enabled: bool
    minimum_cross_section: int
    minimum_industry_coverage: float
    parent_data_universe: str | None = None

    @property
    def contributes_to_overall_verdict(self) -> bool:
        return self.role in {
            ResearchUniverseRole.PRIMARY,
            ResearchUniverseRole.SECONDARY,
        }

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["role"] = self.role.value
        payload["verdict_role"] = self.role.value
        payload["purpose"] = self.purpose.value
        payload["membership_type"] = self.membership_type.value
        payload["factor_publication_mode"] = self.factor_publication_mode.value
        payload["contributes_to_overall_verdict"] = (
            self.contributes_to_overall_verdict
        )
        return payload


__all__ = [
    "FactorPublicationMode",
    "MembershipType",
    "ResearchUniverse",
    "ResearchUniverseRole",
    "UniversePurpose",
]
