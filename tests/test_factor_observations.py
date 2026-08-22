from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import asyncio

import httpx
import pandas as pd
import pytest

from src.data.foundation import MarketDataCatalog, MarketDataReader, MarketDataWriter
from src.factors import artifacts, publication
from src.factors.observations import FactorObservationError, FactorObservationReader
from src.research_universes.models import (
    FactorPublicationMode,
    MembershipType,
    ResearchUniverse,
    ResearchUniverseRole,
    UniversePurpose,
)
from src.research_universes.registry import ResearchUniverseRegistry
from src.utils.io import atomic_save_json, load_json
from src.webapp import research_routes
from src.webapp.app import create_app
from src.webapp.security import AUTH_PASSWORD_ENV, AUTH_USER_ENV


DATES = pd.DatetimeIndex(["2026-07-17", "2026-07-20", "2026-07-21"])


def _bars(ticker: str, _start: str, _end: str) -> pd.DataFrame:
    offset = {"AAA": 0.0, "BBB": 10.0, "CCC": 20.0}[ticker]
    base = pd.Series([100.0 + offset, 101.0 + offset, 102.0 + offset], index=DATES)
    return pd.DataFrame(
        {
            "open": base,
            "high": base + 2.0,
            "low": base - 1.0,
            "close": base + 1.0,
            "adj_close": base + 1.0,
            "volume": 1_000.0,
        },
        index=DATES,
    )


def _registry(tmp_path: Path) -> ResearchUniverseRegistry:
    return ResearchUniverseRegistry(
        {
            "TEST": ResearchUniverse(
                universe_id="TEST",
                display_name="Test",
                purpose=UniversePurpose.VALIDATION,
                role=ResearchUniverseRole.PRIMARY,
                membership_type=MembershipType.PIT,
                factor_publication_mode=FactorPublicationMode.FULL_RESEARCH,
                benchmark="SPY",
                confidence_enabled=False,
                cross_universe_enabled=True,
                minimum_cross_section=3,
                minimum_industry_coverage=0.0,
            )
        },
        source=tmp_path / "registry.yaml",
    )


def _fixture(monkeypatch, tmp_path) -> SimpleNamespace:
    catalog = MarketDataCatalog(tmp_path / "catalog.duckdb")
    writer = MarketDataWriter(
        catalog=catalog,
        lake_dir=tmp_path / "lake",
        fetcher=_bars,
    )
    current = pd.DataFrame(
        {
            "ticker": ["AAA", "CCC"],
            "name": ["Alpha", "Gamma"],
            "sector": ["Technology", "Industrials"],
            "sub_industry": ["Software", "Machinery"],
        }
    )
    membership = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2026-07-17",
                    "2026-07-17",
                    "2026-07-17",
                    "2026-07-20",
                    "2026-07-20",
                    "2026-07-20",
                ]
            ),
            "ticker": ["AAA", "BBB", "CCC", "AAA", "BBB", "CCC"],
            "active": [True, True, False, True, False, True],
        }
    )
    result = writer.update_universe(
        "TEST",
        target_session="2026-07-21",
        universe_frame=current,
        membership_frame=membership,
        membership_source="unit-test",
        initial_start="2026-07-17",
    )

    factor_root = tmp_path / "factor-output"
    monkeypatch.setattr(
        artifacts,
        "factor_values_path",
        lambda name, universe="SP500": (
            factor_root / universe / "factors" / name / "factor_values.parquet"
        ),
    )
    monkeypatch.setattr(
        artifacts,
        "factor_raw_values_path",
        lambda name, universe="SP500": (
            factor_root
            / universe
            / "factors"
            / name
            / "factor_raw_values.parquet"
        ),
    )
    publication_root = tmp_path / "research-output"
    monkeypatch.setattr(publication, "_output_root", lambda: publication_root)
    monkeypatch.setattr(
        publication,
        "factor_bundle_manifest_path",
        lambda factor_id, universe: artifacts.factor_bundle_manifest_path(
            factor_id, universe
        ),
    )

    provenance = publication.dataset_version_provenance(result.version)
    raw_mom = pd.DataFrame(
        {
            "AAA": [float("nan"), 2.0, 3.0],
            "BBB": [1.0, -9.0, -8.0],
            "CCC": [9.0, 2.0, 1.0],
        },
        index=DATES,
    )
    clean_mom = raw_mom.copy()
    raw_vol = pd.DataFrame(
        {
            "AAA": [0.3, 0.2, 0.2],
            "BBB": [0.1, 9.0, 9.0],
            "CCC": [0.9, 0.4, 0.4],
        },
        index=DATES,
    )
    clean_vol = raw_vol.copy()
    clean_vol.loc[pd.Timestamp("2026-07-21"), "CCC"] = float("nan")
    for factor_id, raw, clean, direction in (
        ("MOM_1M", raw_mom, clean_mom, 1),
        ("VOL_20D", raw_vol, clean_vol, -1),
    ):
        artifacts.save_factor_matrix_bundle(
            factor_id,
            raw=raw,
            clean=clean,
            universe="TEST",
            provenance={
                "factor_direction": direction,
                "data_foundation": provenance,
            },
        )
    publication.publish_factor_research(
        universe="TEST",
        version=result.version,
        factor_ids=["MOM_1M", "VOL_20D"],
    )
    reader = FactorObservationReader(
        market_reader=MarketDataReader(catalog=catalog),
        registry=_registry(tmp_path),
        expected_session="2026-07-21",
    )
    return SimpleNamespace(
        reader=reader,
        result=result,
        catalog=catalog,
        publication_path=publication.research_publication_path("TEST"),
    )


def test_snapshot_uses_complete_pit_cross_section_before_filtering(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)

    result = env.reader.snapshot(
        universe="TEST",
        factor_id="MOM_1M",
        observation_date="2026-07-21",
        ticker="AAA",
    ).to_dict()

    assert result["summary"]["eligible_count"] == 2
    assert result["total_rows"] == 1
    assert result["rows"][0]["ticker"] == "AAA"
    assert result["rows"][0]["factor_rank"] == 1
    assert result["rows"][0]["eligible_count"] == 2
    assert result["rows"][0]["factor_percentile"] == 100.0


def test_snapshot_pagination_keeps_full_cross_section_rank(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)

    first_page = env.reader.snapshot(
        universe="TEST",
        factor_id="MOM_1M",
        observation_date="2026-07-20",
        offset=0,
        limit=1,
    ).to_dict()
    second_page = env.reader.snapshot(
        universe="TEST",
        factor_id="MOM_1M",
        observation_date="2026-07-20",
        offset=1,
        limit=1,
    ).to_dict()

    assert first_page["total_rows"] == second_page["total_rows"] == 3
    assert first_page["summary"]["eligible_count"] == 2
    assert second_page["summary"]["eligible_count"] == 2
    assert first_page["rows"][0]["ticker"] == "AAA"
    assert second_page["rows"][0]["ticker"] == "CCC"
    assert first_page["rows"][0]["factor_rank"] == 1
    assert second_page["rows"][0]["factor_rank"] == 1


def test_ties_share_rank_and_non_members_do_not_enter_denominator(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)

    result = env.reader.snapshot(
        universe="TEST",
        factor_id="MOM_1M",
        observation_date="2026-07-20",
    ).to_dict()
    rows = {row["ticker"]: row for row in result["rows"]}

    assert result["summary"]["eligible_count"] == 2
    assert rows["AAA"]["factor_rank"] == 1
    assert rows["CCC"]["factor_rank"] == 1
    assert rows["AAA"]["factor_percentile"] == 75.0
    assert rows["BBB"]["status"] == "NOT_PIT_MEMBER"
    assert rows["BBB"]["factor_rank"] is None
    assert [row["ticker"] for row in result["rows"][:2]] == ["AAA", "CCC"]

    descending = env.reader.snapshot(
        universe="TEST",
        factor_id="MOM_1M",
        observation_date="2026-07-20",
        sort="rank",
        order="desc",
    ).to_dict()
    assert [row["ticker"] for row in descending["rows"][:2]] == ["AAA", "CCC"]


def test_negative_direction_lowest_clean_is_rank_one(monkeypatch, tmp_path):
    env = _fixture(monkeypatch, tmp_path)

    result = env.reader.snapshot(
        universe="TEST",
        factor_id="VOL_20D",
        observation_date="2026-07-17",
    ).to_dict()
    rows = {row["ticker"]: row for row in result["rows"]}

    assert result["contract"]["direction"] == -1
    assert rows["BBB"]["clean_value"] == 0.1
    assert rows["BBB"]["factor_rank"] == 1
    assert rows["AAA"]["factor_rank"] == 2


def test_raw_value_with_missing_clean_is_not_ranked(monkeypatch, tmp_path):
    env = _fixture(monkeypatch, tmp_path)

    result = env.reader.snapshot(
        universe="TEST",
        factor_id="VOL_20D",
        observation_date="2026-07-21",
        status="clean_missing",
    ).to_dict()

    assert result["total_rows"] == 1
    assert result["rows"][0]["ticker"] == "CCC"
    assert result["rows"][0]["raw_value"] == 0.4
    assert result["rows"][0]["clean_value"] is None
    assert result["rows"][0]["factor_rank"] is None
    assert result["rows"][0]["status"] == "CLEAN_MISSING"


def test_history_matches_snapshot_and_preserves_exit_history(monkeypatch, tmp_path):
    env = _fixture(monkeypatch, tmp_path)

    snapshot = env.reader.snapshot(
        universe="TEST",
        factor_id="MOM_1M",
        observation_date="2026-07-17",
    ).to_dict()
    history = env.reader.history(
        universe="TEST",
        factor_id="MOM_1M",
        ticker="BBB",
        start="2026-07-17",
        end="2026-07-21",
    ).to_dict()
    snapshot_bbb = next(row for row in snapshot["rows"] if row["ticker"] == "BBB")

    assert history["rows"][0] == snapshot_bbb
    assert history["rows"][0]["status"] == "VALID"
    assert history["rows"][1]["status"] == "NOT_PIT_MEMBER"
    assert history["rows"][2]["factor_rank"] is None


def test_history_missing_ticker_uses_plain_language_and_alternatives(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(
        env.reader,
        "_ticker_alternatives",
        lambda *_args, **_kwargs: [
            {
                "universe_id": "SECONDARY",
                "role": "SECONDARY",
                "first_pit_member_date": "2024-01-02",
                "last_pit_member_date": "2025-05-16",
                "latest_valid_observation_date": "2025-05-16",
                "current_member": False,
            }
        ],
    )

    with pytest.raises(FactorObservationError) as caught:
        env.reader.history(
            universe="TEST",
            factor_id="MOM_1M",
            ticker="MDB",
        )

    assert caught.value.code == "TICKER_NOT_IN_GENERATION"
    assert "generation" not in str(caught.value)
    assert caught.value.details["selected_universe"] == "TEST"
    assert caught.value.details["available_universes"][0]["universe_id"] == "SECONDARY"


def test_calculation_window_status_and_single_security_percentile(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)

    result = env.reader.snapshot(
        universe="TEST",
        factor_id="MOM_1M",
        observation_date="2026-07-17",
    ).to_dict()
    rows = {row["ticker"]: row for row in result["rows"]}

    assert rows["AAA"]["status"] == "CALCULATION_WINDOW_INSUFFICIENT"
    assert rows["AAA"]["factor_rank"] is None
    assert rows["BBB"]["factor_rank"] == 1
    assert rows["BBB"]["factor_percentile"] == 100.0
    assert rows["BBB"]["quintile"] == "Q5"


def test_non_trading_snapshot_date_does_not_fall_back(monkeypatch, tmp_path):
    env = _fixture(monkeypatch, tmp_path)

    with pytest.raises(FactorObservationError) as caught:
        env.reader.snapshot(
            universe="TEST",
            factor_id="MOM_1M",
            observation_date="2026-07-18",
        )

    assert caught.value.code == "DATE_NOT_AVAILABLE"
    assert caught.value.status_code == 422
    assert caught.value.details == {
        "requested_date": "2026-07-18",
        "previous_date": "2026-07-17",
        "next_date": "2026-07-20",
    }


def test_cache_key_changes_when_publication_changes(monkeypatch, tmp_path):
    env = _fixture(monkeypatch, tmp_path)
    first = env.reader.snapshot(
        universe="TEST", factor_id="MOM_1M", observation_date="latest"
    ).contract.publication_id

    publication.publish_factor_research(
        universe="TEST",
        version=env.result.version,
        factor_ids=["MOM_1M", "VOL_20D"],
    )
    second = env.reader.snapshot(
        universe="TEST", factor_id="MOM_1M", observation_date="latest"
    ).contract.publication_id

    assert second != first


def test_factor_generation_binding_mismatch_fails_closed(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)
    payload = load_json(env.publication_path)
    payload["factors"]["MOM_1M"]["generation_id"] = "tampered-generation"
    atomic_save_json(payload, env.publication_path)

    with pytest.raises(FactorObservationError) as caught:
        env.reader.snapshot(
            universe="TEST",
            factor_id="MOM_1M",
            observation_date="latest",
        )

    assert caught.value.code == "RESEARCH_INVALID"
    assert caught.value.status_code == 409


def test_publication_switch_during_query_restarts_from_one_identity(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)
    original_publication_id = load_json(env.publication_path)["publication_id"]
    original_assert = env.reader._assert_publication_current
    assertion_calls = 0

    def switch_once(contract):
        nonlocal assertion_calls
        assertion_calls += 1
        if assertion_calls == 1:
            publication.publish_factor_research(
                universe="TEST",
                version=env.result.version,
                factor_ids=["MOM_1M", "VOL_20D"],
            )
        return original_assert(contract)

    monkeypatch.setattr(
        env.reader, "_assert_publication_current", switch_once
    )
    result = env.reader.snapshot(
        universe="TEST",
        factor_id="MOM_1M",
        observation_date="latest",
    )
    current_publication_id = load_json(env.publication_path)["publication_id"]

    assert current_publication_id != original_publication_id
    assert result.contract.publication_id == current_publication_id
    assert assertion_calls >= 3


def test_tampered_factor_artifact_fails_closed(monkeypatch, tmp_path):
    env = _fixture(monkeypatch, tmp_path)
    clean_path = artifacts.factor_values_path("MOM_1M", "TEST")
    clean = pd.read_parquet(clean_path)
    clean.iloc[0, 0] = 99.0
    clean.to_parquet(clean_path)

    with pytest.raises(FactorObservationError) as caught:
        env.reader.snapshot(
            universe="TEST",
            factor_id="MOM_1M",
            observation_date="latest",
        )

    assert caught.value.code == "RESEARCH_INVALID"
    assert caught.value.status_code == 409


def test_tampered_membership_version_fails_closed(monkeypatch, tmp_path):
    env = _fixture(monkeypatch, tmp_path)
    membership_path = Path(env.result.version.membership_path)
    membership_path.write_bytes(membership_path.read_bytes() + b"tampered")

    with pytest.raises(FactorObservationError) as caught:
        env.reader.snapshot(
            universe="TEST",
            factor_id="MOM_1M",
            observation_date="latest",
        )

    assert caught.value.code == "RESEARCH_INVALID"
    assert "完整性校验失败" in str(caught.value)


def test_stale_publication_is_explicit_409_state(monkeypatch, tmp_path):
    env = _fixture(monkeypatch, tmp_path)
    stale_reader = FactorObservationReader(
        market_reader=MarketDataReader(catalog=env.catalog),
        registry=_registry(tmp_path),
        expected_session="2026-07-22",
    )

    with pytest.raises(FactorObservationError) as caught:
        stale_reader.snapshot(
            universe="TEST",
            factor_id="MOM_1M",
            observation_date="latest",
        )

    assert caught.value.code == "RESEARCH_STALE"
    assert caught.value.status_code == 409
    assert caught.value.details["target_session"] == "2026-07-21"
    assert caught.value.details["expected_session"] == "2026-07-22"


def test_metadata_reports_unpublished_and_invalid_without_throwing(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)
    meta = env.reader.metadata(
        selected_universe="TEST", selected_factor="MOM_1M"
    )

    assert meta["universes"][0]["status"] == "PUBLISHED"
    assert meta["available_dates"] == [
        "2026-07-17",
        "2026-07-20",
        "2026-07-21",
    ]
    assert meta["ticker_options"] == [
        {"ticker": "AAA", "name": "Alpha"},
        {"ticker": "BBB", "name": "BBB"},
        {"ticker": "CCC", "name": "Gamma"},
    ]
    assert {row["factor_id"] for row in meta["universes"][0]["factors"]} == {
        "MOM_1M",
        "VOL_20D",
    }


def test_factor_data_page_api_export_and_retired_stock_routes(
    monkeypatch, tmp_path
):
    env = _fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(research_routes, "factor_observation_reader", env.reader)
    monkeypatch.delenv(AUTH_USER_ENV, raising=False)
    monkeypatch.delenv(AUTH_PASSWORD_ENV, raising=False)
    app = create_app()

    async def exercise() -> None:
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://quant.test"
        ) as client:
            page = await client.get(
                "/research/factor-data?mode=history&universe=TEST&"
                "factor=MOM_1M&ticker=BBB&start=2026-07-17&end=2026-07-21"
            )
            assert page.status_code == 200
            assert "因子数据" in page.text
            assert '"mode": "history"' in page.text
            assert "日期截面" in page.text
            assert "单股历史" in page.text

            meta = await client.get(
                "/api/research/factor-data/meta?universe=TEST&factor=MOM_1M"
            )
            assert meta.status_code == 200
            assert meta.json()["universes"][0]["status"] == "PUBLISHED"

            snapshot = await client.get(
                "/api/research/factor-data/snapshot?universe=TEST&"
                "factor=MOM_1M&date=2026-07-20&ticker=AAA"
            )
            history = await client.get(
                "/api/research/factor-data/history?universe=TEST&"
                "factor=MOM_1M&ticker=AAA&start=2026-07-20&end=2026-07-20"
            )
            assert snapshot.status_code == history.status_code == 200
            assert snapshot.json()["rows"][0] == history.json()["rows"][0]

            unavailable = await client.get(
                "/api/research/factor-data/snapshot?universe=TEST&"
                "factor=MOM_1M&date=2026-07-18"
            )
            assert unavailable.status_code == 422
            assert unavailable.json()["detail"]["code"] == "DATE_NOT_AVAILABLE"

            exported = await client.get(
                "/api/research/factor-data/export?mode=history&universe=TEST&"
                "factor=MOM_1M&ticker=BBB&start=2026-07-17&end=2026-07-21"
            )
            assert exported.status_code == 200
            assert "factor_data_TEST_MOM_1M_history" in exported.headers[
                "content-disposition"
            ]
            assert "publication_id" in exported.text
            assert "factor_generation_id" in exported.text
            assert "dataset_version_id" in exported.text

            assert (await client.get("/stock/AAPL")).status_code == 404
            assert (await client.get("/api/stock/AAPL")).status_code == 404

    asyncio.run(exercise())
