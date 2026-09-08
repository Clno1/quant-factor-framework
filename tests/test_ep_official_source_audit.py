from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


SPEC = spec_from_file_location("ep_official_probe", Path(__file__).resolve().parents[1] /
                              "reviews/2026-09-08-ep-official-sources/probe.py")
PROBE = module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)


@pytest.mark.parametrize("row,expected", [
    ({"period": "Q2"}, 2), ({"period": "Q4"}, 4), ({"quarter": 2}, 2),
    ({"quarter": "2", "period": "Q2"}, 2), ({"quarter": 1, "period": "Q2"}, None),
    ({"quarter": 2, "period": "FY"}, None), ({"period": "Q5"}, None),
    ({}, None), ({"period": None}, None), ({"period": True}, None),
])
def test_authorized_transcript_period_is_not_assumed_from_request(row, expected):
    assert PROBE.transcript_period(row) == expected
