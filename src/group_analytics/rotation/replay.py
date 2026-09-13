"""Replay private frozen inputs, without provider access or publication."""
from __future__ import annotations

import hashlib
import math
import pandas as pd

from ..artifacts import normalize_json_value
from . import SCHEMA_VERSION
from .engine import analyze
from .store import encoded
from .themes import Theme


def replay_snapshot(snapshot):
    panel = snapshot["input_panel"]
    if hashlib.sha256(encoded(panel)).hexdigest() != snapshot["input_fingerprint"]:
        raise ValueError("Input fingerprint mismatch")
    sessions = pd.DatetimeIndex(panel["sessions"])
    prices = pd.DataFrame(panel["prices"], columns=panel["price_columns"], index=sessions)
    volumes = pd.DataFrame(panel["volumes"], columns=panel["volume_columns"], index=sessions)
    execution = None
    if "execution_close" in panel:
        columns = panel.get("execution_close_columns") or panel["price_columns"]
        execution = pd.DataFrame(panel["execution_close"], columns=columns, index=sessions)
    themes = [Theme(**{**r["definition"], "members": tuple(r["definition"]["members"])}) for r in snapshot["rows"]]
    schema = snapshot.get("schema_version") or SCHEMA_VERSION
    computed = normalize_json_value(analyze(
        prices, volumes, sessions, themes,
        amount_verified=snapshot["amount_verified"],
        execution_close=execution,
        schema_version=schema,
    ))
    differences = []
    for expected, actual in zip(snapshot["rows"], computed):
        for profile in ("production", "compatibility"):
            for field in set(expected[profile]) | set(actual[profile]):
                left, right = expected[profile].get(field), actual[profile].get(field)
                equal = left == right
                if type(left) in (int, float) and type(right) in (int, float):
                    equal = math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-10)
                if not equal:
                    differences.append({"theme": expected["id"], "profile": profile,
                                        "field": field, "stored": left, "replayed": right})
    return {"status": "MATCH" if not differences else "DIFFERENT",
            "source_session": snapshot["source_session"], "run_id": snapshot.get("run_id"),
            "themes": len(themes), "differences": differences,
            "schema_version": schema,
            "scope": "本项目冻结输入重放；不代表TradingView数值对账或预测性验证"}
