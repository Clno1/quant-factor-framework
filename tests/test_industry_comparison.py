import numpy as np
import pandas as pd
import pytest

from src.analysis.industry_comparison import comparison_factors, compare_ic
from src.config import CONFIG
from src.data.classification_history import build_pit_sector_matrix, ClassificationHistoryError


def inputs():
    h = pd.DataFrame([dict(security_id="stable-A", sector="Technology", effective_from="2020-01-01", effective_to=None, knowledge_date="2020-01-01", classification_policy="PIT_EFFECTIVE_DATED", taxonomy="TEST", source="fixture", source_evidence="fixture-evidence")])
    s = pd.DataFrame([dict(security_id="stable-A", ticker="OLD", effective_from="2020-01-01", effective_to="2020-01-02"), dict(security_id="stable-A", ticker="NEW", effective_from="2020-01-03", effective_to=None)])
    return h, s


def test_knowledge_date_and_dated_ticker_identity():
    h, s = inputs()
    dates = pd.date_range("2020-01-01", periods=4)
    m = build_pit_sector_matrix(h, s, dates, pd.Index(["OLD", "NEW"]))
    assert m.loc["2020-01-01"].isna().all()
    assert m.loc["2020-01-02", "OLD"] == "Technology"
    assert pd.isna(m.loc["2020-01-03", "OLD"])
    assert m.loc["2020-01-03", "NEW"] == "Technology"


def test_later_revision_cannot_rewrite_earlier_decision():
    h, s = inputs()
    revision = h.iloc[0].to_dict()
    revision.update(sector="Industrials", knowledge_date="2020-01-03")
    m = build_pit_sector_matrix(pd.concat([h, pd.DataFrame([revision])]), s, pd.date_range("2020-01-02", periods=3), pd.Index(["OLD", "NEW"]))
    assert m.loc["2020-01-03", "NEW"] == "Technology"
    assert m.loc["2020-01-04", "NEW"] == "Industrials"


def test_latest_snapshot_cannot_claim_pit():
    h, s = inputs()
    h["classification_policy"] = "LATEST_KNOWN_BACKFILL_NOT_PIT"
    with pytest.raises(ClassificationHistoryError, match="not PIT"):
        build_pit_sector_matrix(h, s, pd.date_range("2020-01-02", periods=2), pd.Index(["OLD"]))


def test_ambiguous_classification_rejected():
    h, s = inputs()
    conflict = h.copy()
    conflict["sector"] = "Industrials"
    with pytest.raises(ClassificationHistoryError, match="Conflicting"):
        build_pit_sector_matrix(pd.concat([h, conflict]), s, pd.date_range("2020-01-02", periods=2), pd.Index(["OLD"]))


def test_comparison_removes_industry_offsets_preserves_shared_sample_and_config():
    dates = pd.bdate_range("2024-01-02", periods=40)
    columns = pd.Index([f"S{i}" for i in range(60)])
    rng = np.random.default_rng(123)
    within = rng.normal(size=(40, 60))
    raw = pd.DataFrame(within + np.array([10.] * 30 + [-10.] * 30), index=dates, columns=columns)
    sectors = pd.DataFrame([['A'] * 30 + ['B'] * 30] * 40, index=dates, columns=columns)
    sectors.iloc[:, -1] = None
    sectors.attrs["classification_policy"] = "PIT_EFFECTIVE_DATED"
    before = CONFIG.preprocessing.neutralize_industry
    variants, audit = comparison_factors(raw, sectors)
    assert CONFIG.preprocessing.neutralize_industry == before
    pd.testing.assert_frame_equal(variants["baseline_matched"].notna(), variants["industry_neutral"].notna())
    assert variants["industry_neutral"].iloc[:, :30].mean(axis=1).abs().max() < 1e-12
    assert variants["industry_neutral"].iloc[:, 30:-1].mean(axis=1).abs().max() < 1e-12
    assert audit["excluded_observations"] == 40
    returns = pd.DataFrame(rng.normal(0, .01, (40,60)), index=dates, columns=columns)
    ic, summary = compare_ic(variants, returns)
    assert summary.loc["baseline_matched", "N"] == summary.loc["industry_neutral", "N"]
    assert not ic.empty


def test_insufficient_history_blocks_instead_of_filling():
    dates = pd.bdate_range("2024-01-02", periods=2)
    raw = pd.DataFrame(np.ones((2, 40)), index=dates)
    sectors = pd.DataFrame(index=dates, columns=raw.columns)
    sectors.attrs["classification_policy"] = "PIT_EFFECTIVE_DATED"
    with pytest.raises(ClassificationHistoryError, match="coverage"):
        comparison_factors(raw, sectors)


def test_cli_complete_path_uses_same_version_and_leaves_production_unchanged(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import json
    from scripts import run_industry_neutral_comparison as cli

    rng = np.random.default_rng(812)
    dates = pd.bdate_range("2024-01-02", periods=100)
    tickers = pd.Index([f"S{i}" for i in range(60)])
    prices = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, .005, (100, 60)), axis=0)), index=dates, columns=tickers)
    raw = pd.DataFrame(rng.normal(size=(100, 60)) + np.array([3.] * 30 + [-3.] * 30), index=dates, columns=tickers)
    volume = pd.DataFrame(1e7, index=dates, columns=tickers)
    membership = pd.DataFrame({"date": dates[0], "ticker": tickers, "active": True})
    contract = SimpleNamespace(factor_generations={"MOM_6M": "frozen"}, to_dict=lambda: {"dataset_version_id": "fixture"})
    bundle = SimpleNamespace(contract=contract, membership=membership, membership_events=None, wide={"volume": volume}, prices=SimpleNamespace(total_return_close=prices, total_return_open=prices, execution_open=prices, execution_close=prices), benchmark_returns=pd.Series(0., index=dates))
    h = pd.DataFrame([dict(security_id=t, sector="A" if i < 30 else "B", effective_from="2020-01-01", effective_to=None, knowledge_date="2020-01-01", classification_policy="PIT_EFFECTIVE_DATED", taxonomy="TEST", source="fixture", source_evidence="fixture") for i,t in enumerate(tickers)])
    s = pd.DataFrame({"security_id": tickers, "ticker": tickers, "effective_from": "2020-01-01", "effective_to": None})
    h.to_csv(tmp_path / "history.csv", index=False)
    s.to_csv(tmp_path / "symbols.csv", index=False)
    monkeypatch.setattr(cli, "load_published_bundle", lambda **kw: bundle)
    monkeypatch.setattr(cli, "load_factor_matrix_bundle", lambda *a, **kw: (raw, raw, {"generation_id": "frozen"}))
    monkeypatch.setattr("sys.argv", ["run", "--history", str(tmp_path / "history.csv"), "--symbols", str(tmp_path / "symbols.csv"), "--start", str(dates[0].date()), "--end", str(dates[-1].date()), "--output", str(tmp_path / "result")])
    assert cli.main() == 0
    audit = json.loads((tmp_path / "result/audit.json").read_text())
    assert audit["status"] == "COMPLETE"
    assert audit["production_publication_changed"] is False
    assert (tmp_path / "result/industry_neutral_metrics.csv").is_file()
