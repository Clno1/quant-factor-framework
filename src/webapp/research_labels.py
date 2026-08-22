"""Chinese presentation labels for research-domain codes.

The API and persisted publications deliberately keep stable English enum values.
These helpers are only for the HTML presentation layer, so localization cannot
change filtering, version binding, or research calculations.
"""
from __future__ import annotations

import re
from typing import Any


RESEARCH_LABELS = {
    # Cross-universe conclusions.
    "ROBUST": "跨池稳健",
    "PRIMARY_ONLY": "仅主研究池通过",
    "SEGMENT_SPECIFIC": "仅次级验证池通过",
    "CONFLICT": "跨池证据冲突",
    "INSUFFICIENT": "证据不足",
    "REJECT": "不建议采用",
    # Single-universe conclusions and evidence states.
    "PASS": "通过",
    "WATCH": "继续观察",
    "FAIL": "未通过",
    "AVAILABLE": "可用",
    "MISSING": "缺失",
    "STALE": "已过期",
    "INVALID": "无效",
    # Publication and job states.
    "PUBLISHED": "已发布",
    "DEGRADED": "部分异常",
    "RUNNING": "运行中",
    "SUCCESS": "成功",
    "FAILED": "失败",
    "REJECTED": "已拒绝",
    "CANDIDATE_PASS": "候选通过",
    "UNKNOWN": "未知",
    "NOT_APPLICABLE": "不适用",
    "BLOCKED": "未达到正式研究门槛",
    # Factor-observation states.
    "VALID": "有效",
    "NOT_PIT_MEMBER": "非当日成分",
    "CALCULATION_WINDOW_INSUFFICIENT": "计算窗口不足",
    "RAW_MISSING": "原始值缺失",
    "CLEAN_MISSING": "清洗值缺失",
    "CLASSIFICATION_MISSING": "行业分类缺失",
    "DATA_QUALITY_REJECTED": "数据质量未通过",
    # Research-universe metadata.
    "NONE": "不参与跨池结论",
    "PRIMARY": "主研究池",
    "SECONDARY": "次级验证池",
    "REFERENCE": "参考观察池",
    "PIT": "按历史时点变化",
    "STATIC": "固定名单",
    "COVERAGE": "行情覆盖层",
    "ESTIMATION": "宽基比较池",
    "VALIDATION": "稳健性验证池",
    "RAW_ONLY": "仅行情覆盖",
    "FACTOR_DATA": "宽基因子数据",
    "FULL_RESEARCH": "正式完整研究",
}

UNIVERSE_LABELS = {
    "US_EQUITY_COVERAGE": "全美证券行情覆盖",
    "US_LIQUID_5M": "全美流动股票",
    "SP500": "标普 500",
    "NASDAQ100": "纳斯达克 100",
    "MAG7": "科技七巨头",
}

FACTOR_INPUT_LABELS = {
    "open": "开盘价",
    "high": "最高价",
    "low": "最低价",
    "close": "收盘价",
    "adj_close": "复权收盘价",
    "volume": "成交量",
    "returns": "收益率",
    "market_cap": "总市值",
    "sector": "行业分类",
}

HASH_LABELS = {
    "bars_sha256": "日线行情哈希（bars SHA-256）",
    "universe_sha256": "证券信息哈希（universe SHA-256）",
    "membership_sha256": "历史成分哈希（membership SHA-256）",
    "manifest_sha256": "版本清单哈希（manifest SHA-256）",
}

QUALITY_CHECK_LABELS = {
    "bars_integrity": "日线行情完整性",
    "universe_integrity": "证券信息完整性",
    "membership_integrity": "历史成分完整性",
    "classification_coverage": "行业分类覆盖率",
    "current_membership": "当前成分一致性",
    "minimum_cross_section": "最小横截面证券数",
}

TARGET_DATA_STATUS_LABELS = {
    "PUBLISHED": "已就绪",
    "MISSING": "待补齐",
    "STALE": "需更新",
    "INVALID": "校验失败",
}

TARGET_DATA_STATUS_NOTES = {
    "PUBLISHED": "专属行情版本已发布，可以创建回测和模拟盘。",
    "MISSING": "尚无专属行情版本，创建或编辑后会进入补数队列。",
    "STALE": "已有行情版本不是最新交易日，需要执行增量更新。",
    "INVALID": "行情版本的文件或完整性哈希未通过校验。",
}

_TEXT_REPLACEMENTS = (
    ("PRIMARY 与 SECONDARY", "主研究池与次级验证池"),
    ("PRIMARY and SECONDARY", "主研究池与次级验证池"),
    ("DATA_VERSION_MISSING", "数据版本缺失"),
    ("CONFIDENCE_SUMMARY_MISSING", "置信评估摘要缺失"),
    ("CONFIDENCE_VERDICT_INVALID", "单池结论无效"),
    ("EVIDENCE_NOT_PROVIDED", "未提供研究证据"),
)


def research_label(value: Any, default: str = "—") -> str:
    """Return a Chinese label while preserving unknown technical values."""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    return RESEARCH_LABELS.get(text.upper(), text)


def universe_label(value: Any, *, include_code: bool = True) -> str:
    """Return a readable universe name, optionally retaining its stable ID."""
    if value is None:
        return "—"
    code = str(value).strip().upper()
    if not code:
        return "—"
    name = UNIVERSE_LABELS.get(code)
    if not name:
        return code
    return f"{name}（{code}）" if include_code else name


def factor_direction_label(value: Any) -> str:
    """Explain factor direction in investment terms instead of exposing +/-1."""
    try:
        direction = int(value)
    except (TypeError, ValueError):
        return "未配置"
    if direction > 0:
        return "正向：因子值越高，预期收益越高"
    if direction < 0:
        return "负向：因子值越低，预期收益越高"
    return "未配置"


def factor_input_label(value: Any) -> str:
    text = str(value or "").strip()
    return FACTOR_INPUT_LABELS.get(text.lower(), text or "—")


def preprocessing_method_label(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() == "mad":
        return "中位数绝对偏差（MAD）"
    return text or "—"


def hash_label(value: Any) -> str:
    text = str(value or "").strip()
    return HASH_LABELS.get(text, text or "—")


def quality_check_label(value: Any) -> str:
    text = str(value or "").strip()
    return QUALITY_CHECK_LABELS.get(text.lower(), research_text(text))


def target_data_status_label(value: Any) -> str:
    text = str(value or "").strip().upper()
    return TARGET_DATA_STATUS_LABELS.get(text, research_label(text))


def target_data_status_note(value: Any) -> str:
    text = str(value or "").strip().upper()
    return TARGET_DATA_STATUS_NOTES.get(text, "当前行情状态需要进一步检查。")


def research_text(value: Any, default: str = "—") -> str:
    """Translate enum tokens embedded in an existing human-readable message."""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    for source, target in _TEXT_REPLACEMENTS:
        text = text.replace(source, target)
    token_labels = {
        **RESEARCH_LABELS,
        "SP500": "标普500",
        "NASDAQ100": "纳斯达克100",
        "MAG7": "科技七巨头",
    }
    for token in sorted(token_labels, key=len, reverse=True):
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
            token_labels[token],
            text,
        )
    return text.replace(":", "：")


__all__ = [
    "factor_direction_label",
    "factor_input_label",
    "hash_label",
    "preprocessing_method_label",
    "quality_check_label",
    "research_label",
    "research_text",
    "target_data_status_label",
    "target_data_status_note",
    "universe_label",
]
