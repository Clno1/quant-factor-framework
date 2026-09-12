from copy import deepcopy

import pandas as pd
import pytest

from src.data.broad_coverage import normalize_coverage_bars
from src.data.broad_history_repair import fetch_replacement
from src.data.coverage_revisions import (
    classify_revisions, certify_local_revision, frame_fingerprint,
)
from src.data.foundation import DataFoundationError


def bars(dates=("2024-01-29", "2024-01-30", "2024-01-31", "2024-02-01")):
    return normalize_coverage_bars(pd.DataFrame({
        "date": pd.to_datetime(dates), "security_id": "a", "ticker": "A",
        "open": 10., "high": 11., "low": 9., "close": 10., "adj_close": 10.,
        "volume": 100., "unadjusted_close": 10.,
    }), target_session="2024-02-02", ingestion_run_id="fixture")


def inputs():
    old = bars().iloc[:3].copy()
    fresh = bars().iloc[1:].copy()
    fresh.loc[fresh.date.eq("2024-01-31"), "volume"] = 101.
    full = bars()
    full.loc[full.date.eq("2024-01-31"), "volume"] = 101.
    return old, fresh, full


def classify(old, fresh):
    return classify_revisions(old, fresh, parent_target="2024-01-31",
                              window_start="2024-01-30", security_ids=["a"])[0]


def certify(plan, old, fresh, full):
    return certify_local_revision(plan, old, fresh, full,
                                  parent_target="2024-01-31", window_start="2024-01-30")


def test_terminal_revision_remains_a_candidate_until_full_prefix_is_verified():
    old, fresh, full = inputs()
    snapshots = [f.copy(deep=True) for f in (old, fresh, full)]
    plan = classify(old, fresh)
    assert plan["status"] == "LOCAL_REVISION_CANDIDATE"
    assert plan["changes"] == [{"date": "2024-01-31", "fields": {"volume": {"old": 100., "new": 101.}}}]
    result = certify(plan, old, fresh, full)
    assert result["status"] == "VERIFIED_LOCAL_REVISION"
    assert result["retained_rows_checked"] == 2
    assert result["publishable"] is False
    for frame, original in zip((old, fresh, full), snapshots):
        pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("mutation,reason", [
    ("drop_date", "HISTORICAL_DATE_COVERAGE_CHANGED"),
    ("add_date", "HISTORICAL_DATE_COVERAGE_CHANGED"),
    ("symbol", "HISTORICAL_SYMBOL_CHANGED"),
    ("invalid", "INVALID_SOURCE_BAR"),
])
def test_missing_extra_identity_and_bad_bars_are_not_local_revisions(mutation, reason):
    old, fresh, _ = inputs()
    if mutation == "drop_date":
        fresh = fresh.loc[fresh.date.ne("2024-01-30")]
    elif mutation == "add_date":
        old = old.loc[old.date.ne("2024-01-30")]
    elif mutation == "symbol":
        fresh.loc[fresh.date.eq("2024-01-30"), "ticker"] = "OTHER"
    else:
        fresh.loc[fresh.date.eq("2024-01-31"), "volume"] = -1.
    plan = classify(old, fresh)
    assert plan["status"] == "BLOCKED" and reason in plan["reasons"]


def test_earlier_revision_and_no_unchanged_anchor_require_more_evidence():
    old, fresh, _ = inputs()
    fresh.loc[fresh.date.eq("2024-01-30"), "volume"] = 100.000000001
    assert classify(old, fresh)["status"] == "FULL_HISTORY_REQUIRED"
    only_target = old.loc[old.date.eq("2024-01-31")]
    plan = classify(only_target, fresh.loc[fresh.date.ne("2024-01-30")])
    assert plan["status"] == "BLOCKED" and "NO_UNCHANGED_ANCHOR" in plan["reasons"]


@pytest.mark.parametrize("column", ["volume", "adj_close", "unadjusted_close"])
def test_hidden_prefix_revision_is_not_authenticated_by_a_short_window(column):
    old, fresh, full = inputs()
    full.loc[full.date.eq("2024-01-29"), column] += .001
    result = certify(classify(old, fresh), old, fresh, full)
    assert result["status"] == "BLOCKED"
    assert "FULL_PREFIX_NOT_IDENTICAL" in result["reasons"]
    assert result["outside_revision_mismatches"][0]["date"] == "2024-01-29"


@pytest.mark.parametrize("date", ["2024-01-31", "2024-02-01"])
def test_canonical_must_agree_with_frozen_revision_and_new_rows(date):
    old, fresh, full = inputs()
    full.loc[full.date.eq(date), "volume"] += .001
    result = certify(classify(old, fresh), old, fresh, full)
    assert "CANONICAL_AND_FROZEN_SOURCE_DISAGREE" in result["reasons"]


def test_truncated_or_extra_full_history_is_not_certified():
    old, fresh, full = inputs()
    plan = classify(old, fresh)
    result = certify(plan, old, fresh, full.iloc[1:])
    assert result["missing_dates"] == ["2024-01-29"]
    extra = pd.concat([bars(("2024-01-26",)), full], ignore_index=True)
    result = certify(plan, old, fresh, extra)
    assert result["extra_historical_dates"] == ["2024-01-26"]
    assert result["status"] == "BLOCKED"


def test_canonical_new_dates_must_also_exist_in_frozen_source():
    old, fresh, full = inputs()
    fresh = pd.concat([fresh.loc[fresh.date.ne("2024-02-01")], bars(("2024-02-02",))])
    full = pd.concat([full, bars(("2024-02-02",))])
    result = certify(classify(old, fresh), old, fresh, full)
    assert result["extra_new_dates"] == ["2024-02-01"]
    assert result["status"] == "BLOCKED"


@pytest.mark.parametrize("mutation", ["old", "fresh", "plan", "identity", "future", "nominal"])
def test_binding_tamper_and_invalid_certificate_inputs_fail_closed(mutation):
    old, fresh, full = inputs()
    plan = classify(old, fresh)
    if mutation == "old":
        old.loc[old.date.eq("2024-01-30"), "volume"] += .01
    elif mutation == "fresh":
        fresh.loc[fresh.date.eq("2024-01-31"), "volume"] += .01
    elif mutation == "plan":
        plan = deepcopy(plan); plan["changes"][0]["fields"]["volume"]["new"] = 102.
    elif mutation == "identity":
        full["security_id"] = "other"
    elif mutation == "future":
        full = pd.concat([full, bars(("2024-02-02",))], ignore_index=True)
    else:
        full["unadjusted_close"] = 0.
    with pytest.raises(DataFoundationError):
        certify(plan, old, fresh, full)


def test_duplicates_and_timezone_dates_are_not_silently_normalized():
    old, fresh, _ = inputs()
    with pytest.raises(DataFoundationError, match="duplicate"):
        classify(old, pd.concat([fresh, fresh.iloc[:1]], ignore_index=True))
    fresh["date"] = fresh.date.dt.tz_localize("UTC")
    with pytest.raises(DataFoundationError, match="timezone-naive"):
        classify(old, fresh)


def test_fingerprint_is_order_independent_but_preserves_small_revisions():
    original = bars()
    assert frame_fingerprint(original) == frame_fingerprint(original.iloc[::-1])
    changed = original.copy(); changed.loc[0, "volume"] += 1e-12
    assert frame_fingerprint(original) != frame_fingerprint(changed)


def cache_inputs(tmp_path):
    old, fresh, _ = inputs()
    return dict(
        cache_dir=tmp_path, contract={"parent": "fixture"}, security_id="a",
        universe=pd.DataFrame([{"security_id": "a", "current_ticker": "A",
                               "coverage_start": pd.Timestamp("2024-01-29"),
                               "listing_date": pd.Timestamp("2024-01-29"), "delisting_date": pd.NaT}]),
        symbols=pd.DataFrame([{"security_id": "a", "ticker": "A",
                               "effective_from": "2024-01-29", "effective_to": None}]),
        previous=old, recent=fresh, history_start="2024-01-29", target=pd.Timestamp("2024-02-01"),
    )


def test_cache_only_missing_replacement_does_not_fetch_or_create_files(tmp_path):
    with pytest.raises(DataFoundationError, match="cache is missing"):
        fetch_replacement(**cache_inputs(tmp_path), cache_only=True,
                          fetcher=lambda *_: pytest.fail("cache-only must never request provider data"))
    assert list(tmp_path.iterdir()) == []


def test_cache_only_revalidates_existing_evidence_without_mutation(tmp_path):
    kw = cache_inputs(tmp_path)
    path, _ = fetch_replacement(**kw, fetcher=lambda *_: bars().set_index("date"))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    cached, proof = fetch_replacement(
        **kw, fetcher=lambda *_: pytest.fail("read-only cache attempted a fetch"), cache_only=True,
    )
    assert cached == path and proof["cache_hit"] is True
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(DataFoundationError, match="hash mismatch"):
        fetch_replacement(**kw, fetcher=lambda *_: pytest.fail("corruption must not refetch"), cache_only=True)


@pytest.mark.parametrize("options", [
    ["--publish"],
    ["--verify-ticker", "A", "--verify-ticker", "a"],
    [part for i in range(9) for part in ("--verify-ticker", f"A{i}")],
])
def test_cli_has_no_publication_or_unbounded_verification(options):
    from scripts.audit_coverage_revisions import _parse_args

    with pytest.raises(SystemExit):
        _parse_args(["--scope-audit", "unused", "--scope-sha256", "unused", *options])


def test_wrong_scope_digest_fails_before_opening_other_inputs(tmp_path):
    from scripts.audit_coverage_revisions import load_inputs

    scope = tmp_path / "scope.json"
    scope.write_text("{}")
    with pytest.raises(DataFoundationError, match="SHA-256 mismatch"):
        load_inputs(scope, "wrong")
