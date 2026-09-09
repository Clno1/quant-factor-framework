from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.data.foundation import DatasetVersion
from src.data.price_semantics import build_price_semantics_contract
import src.factors.publication as publication
from src.utils.io import atomic_save_json


def _version(version_id: str = "v1") -> DatasetVersion:
    return DatasetVersion(
        version_id=version_id,
        run_id=f"run-{version_id}",
        universe="TEST",
        provider="fmp",
        status="PUBLISHED",
        target_session=date(2026, 7, 31),
        created_at=datetime.now(timezone.utc),
        row_count=10,
        ticker_count=2,
        min_date=date(2026, 1, 1),
        max_date=date(2026, 7, 31),
        target_coverage=1.0,
        bars_path="bars.parquet",
        universe_path="universe.parquet",
        membership_path="membership.parquet",
        membership_checksum_sha256="membership-sha",
        manifest_path="manifest.json",
        checksum_sha256="bars-sha",
        universe_checksum_sha256="universe-sha",
        manifest_checksum_sha256="manifest-sha",
    )


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(publication, "_output_root", lambda: tmp_path)
    monkeypatch.setattr(
        publication,
        "factor_bundle_manifest_path",
        lambda factor_id, universe: (
            tmp_path / universe / "factors" / factor_id / "manifest.json"
        ),
    )
    monkeypatch.setattr(
        publication.MarketDataReader,
        "verify_version",
        lambda self, version, **kwargs: {
            "price_semantics": build_price_semantics_contract(
                source="TEST_CANONICAL_FIXTURE",
                history_mode="FULL_REBUILD",
            )
        },
    )


def _write_factor_manifest(tmp_path, version, generation="g1"):
    path = tmp_path / "TEST" / "factors" / "MOM" / "manifest.json"
    atomic_save_json(
        {
            "generation_id": generation,
            "date_start": "2026-01-01",
            "date_end": "2026-07-31",
            "provenance": {
                "data_foundation": publication.dataset_version_provenance(version)
            },
        },
        path,
    )
    return path


def _write_confidence(tmp_path, *, verdict="PASS"):
    path = tmp_path / "universes" / "TEST" / "factors" / "MOM" / "confidence.json"
    atomic_save_json(
        {
            "factor": "MOM",
            "verdict": verdict,
            "methodology_version": publication.CONFIDENCE_METHODOLOGY_VERSION,
            "generated_at": "2026-07-31T23:00:00Z",
            "summary": {},
        },
        path,
    )
    return path


def test_research_publication_binds_all_factors_to_one_data_version(
    monkeypatch,
    tmp_path,
):
    _redirect(monkeypatch, tmp_path)
    version = _version()
    _write_factor_manifest(tmp_path, version)

    path = publication.publish_factor_research(
        universe="TEST",
        version=version,
        factor_ids=["MOM"],
    )
    loaded = publication.validate_factor_research_publication(
        "TEST",
        version=version,
        factor_ids=["MOM"],
    )

    assert path.exists()
    assert loaded["data_foundation"]["version_id"] == "v1"
    assert loaded["factors"]["MOM"]["generation_id"] == "g1"


def test_research_publication_rejects_factor_changed_after_completion(
    monkeypatch,
    tmp_path,
):
    _redirect(monkeypatch, tmp_path)
    version = _version()
    _write_factor_manifest(tmp_path, version)
    publication.publish_factor_research(
        universe="TEST",
        version=version,
        factor_ids=["MOM"],
    )
    _write_factor_manifest(tmp_path, version, generation="g2")

    with pytest.raises(
        publication.ResearchPublicationError,
        match="changed after publication",
    ):
        publication.validate_factor_research_publication(
            "TEST",
            version=version,
            factor_ids=["MOM"],
        )


def test_research_publication_rejects_stale_market_data_version(
    monkeypatch,
    tmp_path,
):
    _redirect(monkeypatch, tmp_path)
    version = _version("v1")
    _write_factor_manifest(tmp_path, version)
    publication.publish_factor_research(
        universe="TEST",
        version=version,
        factor_ids=["MOM"],
    )

    with pytest.raises(
        publication.ResearchPublicationError,
        match="stale",
    ):
        publication.validate_factor_research_publication(
            "TEST",
            version=_version("v2"),
            factor_ids=["MOM"],
        )


def test_research_publication_binds_confidence_report(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(publication, "_confidence_required", lambda _universe: True)
    version = _version()
    _write_factor_manifest(tmp_path, version)
    _write_confidence(tmp_path)
    publication.publish_factor_research(
        universe="TEST",
        version=version,
        factor_ids=["MOM"],
    )

    _write_confidence(tmp_path, verdict="FAIL")
    with pytest.raises(
        publication.ResearchPublicationError,
        match="Confidence report changed after publication",
    ):
        publication.validate_factor_research_publication(
            "TEST",
            version=version,
            factor_ids=["MOM"],
        )
