from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd
import pytest

from src.data.foundation import (
    DataFoundationError,
    DatasetVersion,
    MarketDataCatalog,
    MarketDataReader,
    MarketDataWriter,
    QualityCheck,
)
from src.data.security_master_store import SecurityMasterGeneration
from src.data.universe_publication import DerivedUniverseStore
from src.factors import get_factor
from src.factors.broad_pipeline import (
    INPUT_FINGERPRINT_METHOD,
    compute_factor_block,
    factor_input_fingerprint,
    factor_history_sessions,
    output_months,
)
from src.factors.broad_observations import BroadFactorObservationBackend
from src.factors.data_publication import FactorDataStore


def _sessions(start: str, end: str) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    values = xcals.get_calendar("XNYS").sessions_in_range(start, end)
    if values.tz is not None:
        values = values.tz_localize(None)
    return pd.DatetimeIndex(values).normalize()


def _broad_factor_fixture() -> tuple[
    pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DatetimeIndex
]:
    sessions = _sessions("2019-12-02", "2020-02-28")
    output = sessions[sessions.to_period("M") == pd.Period("2020-02")]
    rows: list[dict] = []
    master_rows: list[dict] = []
    classifications: list[dict] = []
    for number in range(35):
        security_id = f"sec_{number:03d}"
        ticker = f"T{number:03d}"
        first = len(sessions) - 10 if number == 33 else 0
        rate = 0.0005 + number * 0.00003
        for position, session in enumerate(sessions[first:], start=first):
            price = 10.0 * (1.0 + rate) ** position
            rows.append({
                "date": session,
                "security_id": security_id,
                "ticker": ticker,
                "adj_close": price,
                "volume": 1_000_000.0 + number * 1_000.0,
            })
        master_rows.append({
            "security_id": security_id,
            "current_ticker": ticker,
            "name": ticker,
        })
        classifications.append({
            "security_id": security_id,
            "sector": "UNKNOWN" if number == 32 else f"SECTOR_{number % 3}",
            "knowledge_date": "2020-02-28",
        })
    membership = pd.DataFrame([
        {
            "date": "2019-12-31",
            "security_id": f"sec_{number:03d}",
            "ticker": f"T{number:03d}",
            "active": True,
        }
        for number in range(34)
    ])
    return (
        pd.DataFrame(rows),
        membership,
        pd.DataFrame(master_rows),
        pd.DataFrame(classifications),
        output,
    )


def test_broad_factor_block_keeps_coverage_raw_and_cleans_only_pit_members():
    bars, membership, master, classifications, output = _broad_factor_fixture()
    result = compute_factor_block(
        factor_id="MOM_1M",
        bars=bars,
        membership=membership,
        master=master,
        classifications=classifications,
        output_dates=output,
    )
    latest = result.observations.loc[
        result.observations["date"].eq(output.max())
    ].set_index("security_id")

    assert latest.loc["sec_034", "status"] == "NOT_PIT_MEMBER"
    assert np.isfinite(latest.loc["sec_034", "raw_value"])
    assert pd.isna(latest.loc["sec_034", "clean_value"])
    assert latest.loc["sec_033", "status"] == "CALCULATION_WINDOW_INSUFFICIENT"
    assert latest.loc["sec_032", "status"] == "CLASSIFICATION_MISSING"
    assert np.isfinite(latest.loc["sec_032", "clean_value"])
    assert result.diagnostics["latest_member_count"] == 34
    assert result.diagnostics["latest_warmup_eligible_count"] == 33
    assert result.diagnostics["latest_raw_coverage"] == 1.0
    assert result.diagnostics["latest_clean_coverage"] == 1.0

    prices = bars.pivot(index="date", columns="security_id", values="adj_close")
    expected = get_factor("MOM_1M").compute(prices).at[output.max(), "sec_000"]
    assert latest.loc["sec_000", "raw_value"] == pytest.approx(expected)


def test_broad_factor_windows_and_month_blocks_match_registered_formulas():
    assert factor_history_sessions("MOM_1M") == 21
    assert factor_history_sessions("MOM_3M") == 84
    assert factor_history_sessions("MOM_6M") == 147
    assert factor_history_sessions("MOM_12M") == 273
    assert factor_history_sessions("VOL_60D") == 60
    assert factor_history_sessions("REVERSAL") == 5
    assert factor_history_sessions("TURNOVER") == 20
    blocks = output_months("2020-01-15", "2020-03-03")
    assert [(start.month, end.month) for start, end in blocks] == [
        (1, 1), (2, 2), (3, 3)
    ]


def test_turnover_zero_volume_window_stays_numeric_and_missing():
    bars, membership, master, classifications, output = _broad_factor_fixture()
    bars.loc[bars["security_id"].eq("sec_000"), "volume"] = 0.0

    result = compute_factor_block(
        factor_id="TURNOVER",
        bars=bars,
        membership=membership,
        master=master,
        classifications=classifications,
        output_dates=output,
    )
    latest = result.observations.loc[
        result.observations["date"].eq(output.max())
    ].set_index("security_id")

    assert pd.isna(latest.loc["sec_000", "raw_value"])
    assert latest.loc["sec_000", "status"] == "RAW_MISSING"
    assert pd.api.types.is_float_dtype(result.observations["raw_value"])


def test_broad_factor_block_rejects_non_xnys_input_dates():
    bars, membership, master, classifications, output = _broad_factor_fixture()
    bad = bars.iloc[[0]].copy()
    bad["date"] = pd.Timestamp("2019-12-01")
    bars = pd.concat([bars, bad], ignore_index=True)

    with pytest.raises(DataFoundationError, match="non-XNYS sessions"):
        compute_factor_block(
            factor_id="MOM_1M",
            bars=bars,
            membership=membership,
            master=master,
            classifications=classifications,
            output_dates=output,
        )


def test_factor_input_fingerprint_ignores_version_ids_but_detects_real_inputs(tmp_path):
    def parent(version_id: str, index_name: str, part_hash: str) -> DatasetVersion:
        index_path = tmp_path / index_name
        index_path.write_text(
            __import__("json").dumps({
                "schema_version": 1,
                "storage_type": "PARTITIONED_PARQUET_V1",
                "version_id": version_id,
                "partitions": [{
                    "file": "bars/year=2020/part.parquet",
                    "sha256": part_hash,
                    "rows": 100,
                    "min_date": "2020-01-02",
                    "max_date": "2020-03-31",
                }],
            }),
            encoding="utf-8",
        )
        return DatasetVersion(
            version_id=version_id,
            run_id=version_id,
            universe="US_EQUITY_COVERAGE",
            provider="fmp",
            status="PUBLISHED",
            target_session=date(2020, 3, 31),
            created_at=datetime.now(timezone.utc),
            row_count=100,
            ticker_count=2,
            min_date=date(2020, 1, 2),
            max_date=date(2020, 3, 31),
            target_coverage=1.0,
            bars_path=str(index_path),
            universe_path="unused.parquet",
            membership_path=None,
            membership_checksum_sha256=None,
            manifest_path="unused.json",
            checksum_sha256="index",
            universe_checksum_sha256="universe",
            manifest_checksum_sha256="manifest",
        )

    membership = pd.DataFrame([{
        "date": "2020-02-28",
        "security_id": "sec_aaa",
        "ticker": "AAA",
        "active": True,
        "snapshot_type": "MONTH_END",
        "source_data_version_id": "old",
    }])
    classifications = pd.DataFrame([{
        "security_id": "sec_aaa",
        "sector": "Technology",
        "classification_policy": "LATEST_KNOWN_BACKFILL_NOT_PIT",
        "knowledge_date": "2020-03-31",
    }])
    first, _ = factor_input_fingerprint(
        factor_id="MOM_1M",
        parent_version=parent("v1", "v1.json", "same-bars"),
        membership=membership,
        classifications=classifications,
        output_start="2020-03-02",
        output_end="2020-03-31",
    )
    rebound = membership.assign(source_data_version_id="new")
    second, _ = factor_input_fingerprint(
        factor_id="MOM_1M",
        parent_version=parent("v2", "v2.json", "same-bars"),
        membership=rebound,
        classifications=classifications,
        output_start="2020-03-02",
        output_end="2020-03-31",
    )
    changed, _ = factor_input_fingerprint(
        factor_id="MOM_1M",
        parent_version=parent("v3", "v3.json", "revised-bars"),
        membership=rebound,
        classifications=classifications,
        output_start="2020-03-02",
        output_end="2020-03-31",
    )
    assert first == second
    assert changed != first
    assert INPUT_FINGERPRINT_METHOD == "BROAD_FACTOR_INPUT_V2_XNYS_ONLY"


def _standard_bars(ticker: str, dates: list[str]) -> pd.DataFrame:
    index = pd.to_datetime(dates)
    return pd.DataFrame({
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.0,
        "adj_close": 10.0,
        "volume": 1_000_000.0,
    }, index=index)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_factor_data_publication_binds_all_parents_and_rejects_tampering():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        catalog = MarketDataCatalog(root / "catalog.duckdb")
        market_reader = MarketDataReader(catalog=catalog)
        writer = MarketDataWriter(
            catalog=catalog,
            lake_dir=root / "market",
            fetcher=lambda ticker, start, end: _standard_bars(
                ticker, ["2020-03-31"]
            ),
            fetcher_semantics_source="TEST_CANONICAL_FIXTURE",
        )
        parent = writer.update_universe(
            "US_EQUITY_COVERAGE",
            target_session="2020-03-31",
            initial_start="2020-03-31",
            universe_frame=pd.DataFrame({
                "ticker": ["AAA", "BBB"],
                "name": ["Alpha", "Beta"],
            }),
        ).version
        assert parent is not None

        security_manifest = root / "security_manifest.json"
        security_manifest.write_text(
            '{"generation_id":"security-v1"}', encoding="utf-8"
        )
        security = SecurityMasterGeneration(
            generation_id="security-v1",
            target_session=date(2020, 3, 31),
            created_at=datetime.now(timezone.utc),
            status="PUBLISHED",
            row_count=2,
            active_count=2,
            master_path=str(root / "master.parquet"),
            symbols_path=str(root / "symbols.parquet"),
            classifications_path=str(root / "classifications.parquet"),
            identity_keys_path=str(root / "keys.parquet"),
            manifest_path=str(security_manifest),
            master_sha256="master",
            symbols_sha256="symbols",
            classifications_sha256="classifications",
            identity_keys_sha256="keys",
            manifest_sha256=_sha256(security_manifest),
        )
        universe_store = DerivedUniverseStore(
            catalog=catalog,
            snapshot_root=root / "universes",
            market_reader=market_reader,
        )
        membership = pd.DataFrame([
            {
                "date": "2020-03-31",
                "security_id": security_id,
                "ticker": ticker,
                "active": True,
                "selection_price": 10.0,
                "adv20_usd": 10_000_000.0,
                "valid_sessions_20d": 20,
                "asset_type_pass": True,
                "price_pass": True,
                "liquidity_pass": True,
                "reason_codes": "",
                "snapshot_type": "MONTH_END",
                "source_data_version_id": parent.version_id,
            }
            for security_id, ticker in (("sec_aaa", "AAA"), ("sec_bbb", "BBB"))
        ])
        eligibility = membership.rename(columns={"active": "eligible"}).copy()
        universe_version = universe_store.publish(
            universe="US_LIQUID_5M",
            parent_version=parent,
            security_master=security,
            membership=membership,
            eligibility=eligibility,
            methodology_version="US_LIQUID_5M_PIT_V1",
            checks=[QualityCheck("all", True, 1, 1, "passed")],
        )

        store = FactorDataStore(
            output_root=root / "factor_data",
            market_reader=market_reader,
            universe_store=universe_store,
        )
        generation_id = store.new_generation_id()
        early_partition = store.write_partition(
            pd.DataFrame([
                {
                    "date": "2020-01-31",
                    "security_id": security_id,
                    "ticker": ticker,
                    "factor_id": "MOM_1M",
                    "raw_value": raw,
                    "clean_value": clean,
                    "pit_member": True,
                    "status": "VALID",
                }
                for security_id, ticker, raw, clean in (
                    ("sec_aaa", "AAA", 0.1, -0.5),
                    ("sec_bbb", "BBB", 0.2, 0.5),
                )
            ]),
            generation_id=generation_id,
            factor_id="MOM_1M",
            target_session="2020-03-31",
        )
        partition = store.write_partition(
            pd.DataFrame([
                {
                    "date": "2020-03-31",
                    "security_id": security_id,
                    "ticker": ticker,
                    "factor_id": "MOM_1M",
                    "raw_value": raw,
                    "clean_value": clean,
                    "pit_member": True,
                    "status": "VALID",
                }
                for security_id, ticker, raw, clean in (
                    ("sec_aaa", "AAA", 0.2, 0.5),
                    ("sec_bbb", "BBB", 0.1, -0.5),
                )
            ]),
            generation_id=generation_id,
            factor_id="MOM_1M",
            target_session="2020-03-31",
        )
        negative_partition = store.write_partition(
            pd.DataFrame([
                {
                    "date": "2020-03-31",
                    "security_id": security_id,
                    "ticker": ticker,
                    "factor_id": "VOL_20D",
                    "raw_value": raw,
                    "clean_value": clean,
                    "pit_member": True,
                    "status": "VALID",
                }
                for security_id, ticker, raw, clean in (
                    ("sec_aaa", "AAA", 0.2, 0.5),
                    ("sec_bbb", "BBB", 0.1, -0.5),
                )
            ]),
            generation_id=generation_id,
            factor_id="VOL_20D",
            target_session="2020-03-31",
        )
        pointer = store.publish(
            generation_id=generation_id,
            universe="US_LIQUID_5M",
            parent_version=parent,
            universe_version=universe_version,
            security_master=security,
            factor_partitions={
                "MOM_1M": [early_partition, partition],
                "VOL_20D": [negative_partition],
            },
            factor_metadata={
                "MOM_1M": {
                    "direction": 1,
                    "factor_module": "src.factors.momentum",
                    "factor_class": "Momentum1M",
                    "factor_parameters": {"lookback": 21, "skip": 0},
                },
                "VOL_20D": {
                    "direction": -1,
                    "factor_module": "src.factors.volatility",
                    "factor_class": "Volatility20D",
                    "factor_parameters": {"window": 20},
                },
            },
            checks=[QualityCheck("all", True, 1, 1, "passed")],
            methodology_version="BROAD_FACTOR_DATA_V1",
            preprocessing_methodology_version="FACTOR_CLEAN_V1",
            classification_policy="LATEST_KNOWN_BACKFILL_NOT_PIT",
            required_factor_ids=["MOM_1M", "VOL_20D"],
            require_input_fingerprints=False,
        )
        assert store.load_publication() == pointer
        entries = store.partition_entries(pointer, "MOM_1M")
        assert len(entries) == 2

        security_frames = {
            "master": pd.DataFrame([
                {"security_id": "sec_aaa", "current_ticker": "AAA", "name": "Alpha"},
                {"security_id": "sec_bbb", "current_ticker": "BBB", "name": "Beta"},
            ]),
            "symbols": pd.DataFrame([
                {"security_id": "sec_aaa", "ticker": "AAA", "effective_from": "2010-01-01", "effective_to": None},
                {"security_id": "sec_bbb", "ticker": "BBB", "effective_from": "2010-01-01", "effective_to": None},
            ]),
            "classifications": pd.DataFrame([
                {"security_id": "sec_aaa", "sector": "Technology", "knowledge_date": "2020-03-31"},
                {"security_id": "sec_bbb", "sector": "Industrials", "knowledge_date": "2020-03-31"},
            ]),
        }
        backend = BroadFactorObservationBackend(
            store=store,
            security_loader=lambda: (security, security_frames),
            expected_session="2020-03-31",
        )
        snapshot = backend.snapshot(
            factor_id="MOM_1M", ticker="AAA", limit=10
        )
        assert snapshot.total_rows == 1
        assert snapshot.generation_total_rows == 2
        assert snapshot.rows[0]["security_id"] == "sec_aaa"
        assert snapshot.rows[0]["factor_rank"] == 1
        assert snapshot.rows[0]["eligible_count"] == 2
        assert snapshot.rows[0]["factor_percentile"] == 100.0
        history = backend.history(factor_id="MOM_1M", ticker="AAA")
        assert history.security_id == "sec_aaa"
        assert [row["date"] for row in history.rows] == [
            "2020-01-31",
            "2020-03-31",
        ]
        assert [row["factor_rank"] for row in history.rows] == [2, 1]
        metadata = backend.metadata(selected_factor="MOM_1M")
        assert metadata["universe"]["factor_data_status"] == "PUBLISHED"
        assert metadata["universe"]["web_default_enabled"] is True
        assert metadata["universe"]["research_status"] == "BLOCKED"
        assert metadata["universe"]["research_blockers"] == [
            "PIT_CLASSIFICATION_POLICY"
        ]
        assert metadata["universe"]["capabilities"] == {
            "raw": True,
            "clean": True,
            "rank": True,
            "confidence": False,
        }
        assert metadata["available_dates"] == ["2020-03-31"]
        search = backend.search_securities(query="Alpha", asof="2020-03-31")
        assert search["rows"][0]["security_id"] == "sec_aaa"
        assert search["rows"][0]["ticker"] == "AAA"
        assert search["rows"][0]["coverage_status"] == "PUBLISHED"
        assert search["rows"][0]["available_comparison_universes"] == [
            "US_LIQUID_5M"
        ]
        negative = backend.snapshot(
            factor_id="VOL_20D", ticker="BBB", limit=10
        )
        assert negative.rows[0]["factor_rank"] == 1
        assert negative.rows[0]["factor_percentile"] == 100.0

        partition_path = Path(entries[-1].path)
        partition_path.write_bytes(partition_path.read_bytes() + b"tampered")
        with pytest.raises(DataFoundationError, match="partition hash"):
            store.load_publication()
