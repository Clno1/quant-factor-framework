"""Export archived real model notes and reproduce validation, without API calls."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.breakouts.ep.llm_event import validate_event


def records(audit):
    output = []
    for row in audit["results"]:
        expected = row["result"]["validation"]
        assert validate_event(row["packet"], row["raw_response"]) == expected
        output.append({"ticker": row["ticker"], "request_key": row["request_key"],
                       "packet": row["packet"], "response": row["raw_response"], "expected_validation": expected,
                       "usage": row["result"]["usage"], "reserved_microusd": row["reserved_microusd"]})
    return {"origin": "ACTUAL_KIMI_EVENT_RESPONSES_20260910", "records": output,
            "financial_semantics_verified": False, "external_requests": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--readable", type=Path, required=True)
    args = parser.parse_args()
    audit = json.loads(args.audit.read_text())
    fixture = records(audit)
    with args.fixture.open("x") as output:
        json.dump(fixture, output, ensure_ascii=False, indent=2)
        output.write("\n")
    lines = ["# 真实 Kimi 事件解读（原样留档，未核准、未发送）", "",
             "程序检查仅验证引用 ID 和有限的输出范围，不证明每句都被引用支持。", ""]
    for row in fixture["records"]:
        rejected = {item["index"]: item["reasons"] for item in row["expected_validation"]["rejected"]}
        lines.extend(["## " + row["ticker"], "", "请求：`" + row["request_key"] + "`", ""])
        for i, note in enumerate(row["response"]["notes"]):
            state = ", ".join(rejected[i]) if i in rejected else "通过 ID/范围检查，仍须复核"
            lines.extend([f"### 条目 {i + 1} / {note['kind']}", "", note["text"], "",
                          "引用：" + ", ".join(note["paragraph_ids"]), "检查：" + state, ""])
        lines.extend(["### 本次提供的原文", ""])
        for paragraph in row["packet"]["request"]["untrusted_blocks"]:
            lines.extend([paragraph["paragraph_id"], "", paragraph["text"], ""])
    with args.readable.open("x") as output:
        output.write("\n".join(lines))
