import json
import threading
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from src.data.broad_history_repair import collect_replacements
from src.data.foundation import DataFoundationError
from scripts.update_us_equity_coverage import _validate_repair_options, _refuse_provider_fetch


def failures(n=7):
    return [{"security_id": str(i), "ticker": f"T{i}"} for i in range(n)]


def test_batch_reads_and_parallelism_are_bounded_and_scope_is_explicit(tmp_path):
    reads, active, peak = [], 0, 0
    lock = threading.Lock()

    def load(ids):
        reads.append(ids)
        return pd.DataFrame({"security_id": ids, "date": "2024-01-30"})

    def replace(sid, previous):
        nonlocal active, peak
        assert set(previous.security_id) == {sid}
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(.02)
        with lock:
            active -= 1
        return tmp_path / (sid + ".parquet"), {"cache_hit": True}

    path = tmp_path / "audit.json"
    paths, report = collect_replacements(
        failures=failures(), load_previous=load, replace_security=replace,
        report_path=path, contract={"scope": "hash"}, workers=2, offset=1, limit=5, batch_size=2,
    )
    assert reads == [["1", "2"], ["3", "4"], ["5"]]
    assert peak == 2 and len(paths) == 5
    assert report["status"] == "PREPARED_BATCH" and not report["complete_scope"]
    assert report["total"] == 7 and report["selected_count"] == report["completed"] == 5
    assert [p["security_id"] for p in report["validated"]] == ["1", "2", "3", "4", "5"]
    assert json.loads(path.read_text()) == report


def test_one_failed_security_preserves_evidence_and_blocks_whole_scope(tmp_path):
    def replace(sid, _):
        if sid == "1":
            raise DataFoundationError("missing authenticated history")
        return tmp_path / sid, {"cache_hit": False}

    _, report = collect_replacements(
        failures=failures(3), load_previous=lambda ids: pd.DataFrame({"security_id": ids}),
        replace_security=replace, report_path=tmp_path / "audit.json", contract={}, workers=2,
    )
    assert report["complete_scope"] and report["completed"] == 3
    assert report["status"] == "FAIL" and len(report["validated"]) == 2
    assert report["errors"][0]["security_id"] == "1"


def test_reader_interruption_is_not_left_as_success_or_unexplained_running(tmp_path):
    def load(_):
        raise OSError("parent read interrupted")

    path = tmp_path / "audit.json"
    with pytest.raises(OSError):
        collect_replacements(failures=failures(), load_previous=load,
                             replace_security=lambda *_: pytest.fail("must not fetch"),
                             report_path=path, contract={})
    report = json.loads(path.read_text())
    assert report["status"] == "FAIL" and report["completed"] == 0
    assert report["interruption"] == "parent read interrupted"


def test_foreign_parent_rows_fail_before_provider_calls(tmp_path):
    with pytest.raises(DataFoundationError, match="foreign identities"):
        collect_replacements(failures=failures(1),
                             load_previous=lambda _: pd.DataFrame({"security_id": ["foreign"]}),
                             replace_security=lambda *_: pytest.fail("must not fetch"),
                             report_path=tmp_path / "audit.json", contract={})


def test_missing_parent_identity_cannot_shrink_required_history(tmp_path):
    with pytest.raises(DataFoundationError, match="parent read identity coverage"):
        collect_replacements(failures=failures(2),
                             load_previous=lambda _: pd.DataFrame({"security_id": ["0"]}),
                             replace_security=lambda sid, old: (tmp_path / sid, {"rows": len(old)}),
                             report_path=tmp_path / "audit.json", contract={})


@pytest.mark.parametrize("options", [
    {"publish": True, "repair_only": True},
    {"repair_offset": 1}, {"repair_limit": 1}, {"repair_workers": 3},
    {"repair_only": True, "repair_offset": -1},
    {"repair_only": True, "repair_limit": 0},
    {"repair_cache_only": True},
    {"repair_full_history": False, "repair_only": True},
])
def test_partial_scope_and_unbounded_options_cannot_publish(options):
    args = {"publish": False, "repair_full_history": True, **options}
    with pytest.raises(ValueError):
        _validate_repair_options(SimpleNamespace(**args))


def test_cache_only_provider_function_always_refuses():
    with pytest.raises(DataFoundationError, match="forbids new provider"):
        _refuse_provider_fetch("A", "2024-01-01", "2024-01-31")


def test_duplicate_or_out_of_range_scope_is_rejected(tmp_path):
    for scope, offset in [(failures(1) * 2, 0), (failures(2), 2)]:
        with pytest.raises(DataFoundationError):
            collect_replacements(failures=scope, offset=offset,
                                 load_previous=lambda _: pytest.fail("must not read"),
                                 replace_security=lambda *_: pytest.fail("must not fetch"),
                                 report_path=tmp_path / "audit.json", contract={})
