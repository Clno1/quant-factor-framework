"""Offline comparison of immutable real responses with explicitly reviewed evidence."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.breakouts.ep.llm_event_audit import audit_event
from event_source_review import bind_review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--readable", type=Path, required=True)
    parser.add_argument("--bindings-output", type=Path, required=True)
    args = parser.parse_args()
    paths = [args.output, args.readable, args.bindings_output]
    if len({p.resolve() for p in paths}) != len(paths) or any(p.exists() for p in paths):
        raise ValueError("OUTPUT_MUST_BE_NEW_DISTINCT_FILES")
    source = json.loads(args.fixture.read_text())
    rows, bindings = [], {}
    lines = ["# EP 真实事件解读：离线补强检查", "",
             "零模型请求。标注为本轮逐条对照原文所得，不是自动语义证明。旧结果保持不变。", ""]
    for row in source["records"]:
        review = bind_review(row)
        result = audit_event(row["packet"], row["response"], review)
        bindings[row["ticker"]] = review
        rows.append({"ticker": row["ticker"], "audit": result})
        lines.extend(["## " + row["ticker"], ""])
        for item in result["items"]:
            lines.extend([f"### 条目 {item['index'] + 1}：{item['status']}", "", item["note"]["text"], "",
                          "拦截理由：" + (", ".join(item["reasons"]) or "无已识别硬性问题，仍待审"), ""])
            if item["quantity_expressions"]:
                lines.extend(["数量表达：" + " / ".join(dict.fromkeys(q["text"] for q in item["quantity_expressions"])), ""])
            for claim in item["reviewed_claims"]:
                evidence_ids = sorted({e["paragraph_id"] for e in claim["evidence"]})
                missing = claim["missing_paragraph_ids"]
                lines.append(f"- {claim['text']}：证据 {', '.join(evidence_ids)}；" + ("漏引 " + ", ".join(missing) if missing else "已引用绑定证据"))
            lines.extend(["", "未覆盖文字：" + (" / ".join(s["text"] for s in item["unreviewed_spans"]) or "无；仅指已标注结论范围"), "",
                          "语义自动核准：否；推送：关闭。", ""])
    report = {"external_requests": 0, "delivery": "DISABLED_SHADOW_ONLY", "records": rows}
    for path, content in [(args.output, json.dumps(report, ensure_ascii=False, indent=2) + "\n"),
                          (args.readable, "\n".join(lines)),
                          (args.bindings_output, json.dumps(bindings, ensure_ascii=False, indent=2) + "\n")]:
        with path.open("x") as output:
            output.write(content)
    print(json.dumps({"external_requests": 0, "results": [
        {"ticker": row["ticker"], "statuses": [item["status"] for item in row["audit"]["items"]]}
        for row in rows]}))


if __name__ == "__main__":
    main()
