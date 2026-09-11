"""Export exact prior prompt and offline whole-observation review; no network or keys."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.breakouts.ep.llm_observations import audit_previous_choices, observation_prompt, validate_observation_choice
from src.breakouts.ep.llm_provider import LlmSettings, responses_payload
from src.breakouts.ep.llm_batches import batch_spec
from src.breakouts.ep.models import digest


def run(fixture):
    reports = []
    for row in fixture["records"]:
        report = audit_previous_choices(row["packet"], row["response"])
        catalog = report["catalog"]
        # A deterministic reference choice tests the contract, not model accuracy.
        ids = [u["observation_id"] for u in catalog["observations"] if u["period_role"] == "CURRENT_REPORTED"]
        selection = {"catalog_hash": catalog["catalog_hash"], "scope_status": "UNCERTAIN", "observation_ids": ids}
        report["reference_choice_type"] = "PROGRAM_SELECTED_CURRENT_UNITS_NOT_MODEL_OUTPUT"
        report["reference_choice_validation"] = validate_observation_choice(row["packet"], catalog, selection)
        reports.append({"ticker": row["ticker"], "archived_request_key": row["request_key"], **report})
    return {"reports": reports, "external_requests": 0, "budget_writes": 0,
            "live_model_selection_accuracy": None, "delivery": "DISABLED_SHADOW_ONLY"}


def previous_payload(fixture):
    row = next(r for r in fixture["records"] if r["ticker"] == "PLAB")
    settings = LlmSettings(model="kimi-k2.6", provider="kimi-cn", max_output_tokens=8000, read_timeout_seconds=180)
    request = deepcopy(row["packet"]["request"])
    batch = batch_spec(request["batch"]["name"])
    if batch != request["batch"]:
        raise ValueError("ARCHIVED_BATCH_CHANGED")
    # The archive sorts JSON object keys, but the wire user content is a JSON string.
    # Restore the original builder order before checking the complete request fingerprint.
    request["batch"] = batch
    payload = responses_payload(request, settings)
    if digest({"provider": "kimi-cn", "payload": payload}) != row["request_key"]:
        raise ValueError("ARCHIVED_REQUEST_KEY_MISMATCH")
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    report = run(fixture)
    old = previous_payload(fixture)
    plab = next(r for r in report["reports"] if r["ticker"] == "PLAB")
    preview = observation_prompt(plab["catalog"])
    args.output_dir.mkdir()  # Require a new directory; never overwrite an earlier review.
    for name, content in (("report.json", report), ("plab_actual_request.json", old),
                          ("plab_observation_prompt_preview.json", preview)):
        with (args.output_dir / name).open("x") as output:
            json.dump(content, output, ensure_ascii=False, indent=2)
            output.write("\n")
    request = next(r for r in fixture["records"] if r["ticker"] == "PLAB")["packet"]["request"]
    text = "# PLAB 上轮真实 Prompt 与新原型\n\n"
    text += "完整请求见同目录 `plab_actual_request.json`；从归档目录重建，已核对请求摘要完全一致，无密钥。\n\n"
    text += "## 上轮 system 消息全文\n\n```text\n" + old["messages"][0]["content"] + "\n```\n\n"
    text += "## 上轮 user 消息中的批次重点\n\n```text\n" + request["batch"]["focus"] + "\n```\n\n"
    text += "## 上轮原文段落\n\n"
    for block in request["untrusted_blocks"]:
        text += "### " + block["paragraph_id"] + "\n\n" + block["text"] + "\n\n"
    text += "完整 user 消息还包含片段 ID 目录、版本、覆盖信息，完整 JSON 中也有响应 schema；不能把以上节选当成全部请求。\n\n"
    text += "## 新原型 system（尚未调用模型）\n\n```text\n" + preview["system"] + "\n```\n\n"
    text += "新原型只返回 catalog_hash、scope_status 和 observation_ids，不返回或改写财务字段。"
    with (args.output_dir / "prompt_readable.md").open("x") as output:
        output.write(text + "\n")
    print(json.dumps({"observations": len(plab["catalog"]["observations"]),
                      "current_reference_units": len(plab["reference_choice_validation"]["accepted"]),
                      "original_request_hash_verified": True, "external_requests": 0, "budget_writes": 0}))
