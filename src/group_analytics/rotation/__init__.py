"""Versioned daily rotation research; no dependency on strategy/application domains."""

SCHEMA_VERSION = "rotation.v3.0"
LEGACY_SCHEMA_VERSION = "rotation.v2.1"
READABLE_SCHEMA_VERSIONS = frozenset({LEGACY_SCHEMA_VERSION, SCHEMA_VERSION})
SOURCE_PROFILE = "source_v1_compat"
PRODUCTION_PROFILE = "research_v3"
AMOUNT_BASIS = "split_adjusted_close_x_volume"
AMOUNT_AUDIT_DOC = "docs/sector_rotation_v3_amount_audit_20260912.md"
CACHE_PRICE_BASIS = "canonical_full_plus_dividend_adj"
HOLDINGS_AUDIT_DOC = "docs/sector_rotation_v3_holdings_audit_20260912.md"
FLOWS_AUDIT_DOC = "docs/sector_rotation_v3_flows_audit_20260912.md"
FLOWS_AUDIT_STATUS = "NOT_PASSED"
