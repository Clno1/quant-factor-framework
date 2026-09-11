"""Explicit source-review annotations for two archived cases, not an LLM classifier.

Only the listed assertions were reviewed. Absence claims remain unreviewed.
"""
from copy import deepcopy

from src.breakouts.ep.llm_event import validate_event

PACKETS = {
    "PLAB": "29f2de3938a05bed7ad09ec0d57075e72ce9cbc0420372a861d054a0fd0e20f6",
    "NYAX": "b35f18c7a93710d67aa6d8e22dd3dc5f6c73a2bff3a299077fb738005c923f19",
}

# Source paragraphs are attached in full to retain period, attribution and conditions.
CLAIMS = {
    "PLAB": {1: [
        ("公司同时提供GAAP与Non-GAAP净利润数据", ["p0008", "p0010"]),
        ("Non-GAAP净利润同比持平", ["p0010"]),
        ("环比上升", ["p0010"]),
        ("GAAP净利润同比上升", ["p0008"]),
        ("环比下降", ["p0008"]),
        ("IC与FPD两大业务板块呈现相反走势", ["p0012", "p0014"]),
        ("IC增长而FPD下滑", ["p0012", "p0014"]),
    ]},
    "NYAX": {
        0: [
            ("Nayax（纳斯达克/TASE：NYAX）与Windjammer Capital Investors签署确定性协议", ["p0005"]),
            ("拟以全现金方式收购IPS Group", ["p0005"]),
            ("IPS是智能停车技术提供商", ["p0002", "p0006"]),
            ("拥有超过二十万处停车位的部署基础", ["p0006"]),
            ("Nayax定位为全球商务赋能、支付及忠诚度平台", ["p0005"]),
            ("此次收购旨在将IPS的停车硬件与软件整合至其现有业务体系", ["p0006"]),
        ],
        1: [
            ("公司表示该交易将Nayax的支付基础设施与全球分销网络，同IPS的专用智能停车软硬件相结合", ["p0006"]),
            ("可能加速IPS向欧洲大陆等新市场扩张", ["p0006"]),
            ("并为Nayax现有客户提供停车及路侧管理解决方案", ["p0006"]),
            ("公司还提及存在交叉销售机会", ["p0006"]),
            ('并拟沿用此前收购中"获取-整合-扩展"的策略模式', ["p0006"]),
        ],
        2: [
            ("交易预计于二零二六年第四季度完成", ["p0020"]),
            ("但须获得监管批准并满足常规交割条件", ["p0020"]),
            ("由于存在审批及交割条件未满足的可能性，收购能否如期完成尚不确定", ["p0020"]),
        ],
    },
}


def bind_review(row):
    ticker = row["ticker"]
    if row["packet"]["packet_hash"] != PACKETS[ticker]:
        raise ValueError("SOURCE_REVIEW_PACKET_CHANGED")
    if row["expected_validation"] != validate_event(row["packet"], row["response"]):
        raise ValueError("SOURCE_REVIEW_RESPONSE_CHANGED")
    paragraphs = {p["paragraph_id"]: p["text"] for p in row["packet"]["request"]["untrusted_blocks"]}
    return {"packet_hash": PACKETS[ticker], "origin": "LOCAL_SOURCE_REVIEW", "notes": [
        {"index": index, "original_note": deepcopy(row["response"]["notes"][index]),
         "claims": [{"text": text, "evidence": [{"paragraph_id": pid, "quote": paragraphs[pid]} for pid in pids]}
                    for text, pids in claims]}
        for index, claims in CLAIMS[ticker].items()
    ]}
