"""Optional as-of daily evidence experiment. Never a trading/risk controller.

Only caller-supplied, publication-authorized observations enter the public
snapshot. No automatic download of licensed macro series, inferred causal
narrative, intraday interpolation or fabricated residuals.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math

FAMILIES = {"rates", "dollar", "credit", "growth", "inflation", "liquidity", "events"}


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps must include timezone")
    return parsed.astimezone(timezone.utc)


def evaluate_context(observations, *, cutoff, max_age_days=7):
    cutoff = _time(cutoff)
    accepted, rejected = [], []
    ids = set()
    for item in observations:
        reason = None
        try:
            evidence_id = str(item["id"])
            if evidence_id in ids:
                raise ValueError("duplicate")
            ids.add(evidence_id)
            observed, published, seen = (_time(item[k]) for k in ("observed_at", "published_at", "first_seen_at"))
            score = float(item["strength"])
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("strength")
            if item["family"] not in FAMILIES or item["direction"] not in {"support", "pressure"}:
                raise ValueError("family/direction")
            if item["target_benchmark"] not in {"SPY", "QQQ"}:
                raise ValueError("target_benchmark")
            if not item.get("rule_version") or not item.get("source"):
                raise ValueError("provenance")
            if item.get("publish_allowed") is not True:
                reason = "未确认公开展示许可"
            elif observed > published or seen < observed or max(published, seen) > cutoff:
                reason = "决策时刻尚不可知或时间顺序错误"
            elif (cutoff - observed).total_seconds() > max_age_days * 86400:
                reason = "证据超过有效期"
            if reason is None:
                accepted.append({"id": evidence_id, "family": item["family"],
                                 "target_benchmark": item["target_benchmark"],
                                 "direction": item["direction"], "strength": score,
                                 "observed_at": observed.isoformat(), "known_at": max(published, seen).isoformat(),
                                 "rule_version": str(item["rule_version"]),
                                 "age_days": round((cutoff - observed).total_seconds() / 86400, 2)})
        except (ValueError, KeyError, TypeError, OverflowError):
            reason = "证据格式或来源不合格"
        if reason:
            rejected.append({"reason": reason})  # Do not leak supplied URLs/secrets.
    if not observations:
        return {"status": "not_connected", "label": "背景层未接入", "experimental": True,
                "accepted": [], "rejected": [], "support": None, "pressure": None}
    # One independent family gets at most one vote on each side. Missing is
    # unknown, not a zero opposing score, and no normalized probability exists.
    family_votes = {}
    for item in accepted:
        key = (item["family"], item["direction"])
        family_votes[key] = max(family_votes.get(key, 0), item["strength"])
    support = sum(v >= .7 for (f, d), v in family_votes.items() if d == "support")
    pressure = sum(v >= .7 for (f, d), v in family_votes.items() if d == "pressure")
    if rejected or len({x["family"] for x in accepted}) < 2 or len({x["target_benchmark"] for x in accepted}) != 1:
        status, label = "insufficient", "背景证据不足"
    elif support >= 2 and pressure == 0:
        status, label = "support", "外部证据偏支持（实验）"
    elif pressure >= 2 and support == 0:
        status, label = "pressure", "外部证据偏压制（实验）"
    else:
        status, label = "mixed", "外部证据混合（实验）"
    return {"status": status, "label": label, "experimental": True,
            "target_benchmark": accepted[0]["target_benchmark"] if accepted else None,
            "support": support, "pressure": pressure, "accepted": accepted, "rejected": rejected,
            "cutoff": cutoff.isoformat(), "rule_version": "family-votes-v1",
            "note": "仅描述所供证据；不等于因果、胜率或完整宏观覆盖，不参与主题评分和仓位"}


def price_response(context, row):
    if not row["history_valid"]:
        return "无法核验价格响应"
    if context["status"] in {"not_connected", "insufficient"}:
        return "背景不足，独立观察价格"
    positive = row["abs5"] > 0
    if context["status"] == "support":
        return "价格与支持同向（非因果确认）" if positive else "支撑尚未兑现，等待价格确认"
    if context["status"] == "pressure":
        return "压制背景下价格仍强，存在分歧" if positive else "价格与压制同向（非因果确认）"
    return "背景混合，不给单一动因解释"
