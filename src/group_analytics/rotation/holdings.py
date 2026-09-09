"""Current holdings observation only; never historical membership or a signal input."""
from __future__ import annotations

import hashlib
import math
import re

import pandas as pd

from .engine import clean_table
from .store import encoded


def normalize_observation(rows, symbol, captured_at):
    captured = pd.Timestamp(captured_at)
    if captured.tzinfo is None or not isinstance(rows, list) or not rows:
        raise ValueError("Need dated nonempty holdings observation")
    members, excluded, seen = [], [], set()
    for row in rows:
        if row.get("symbol") != symbol:
            raise ValueError("Holding fund mismatch")
        asset = str(row.get("asset") or "")
        try:
            weight = float(row["weightPercentage"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Missing holding weight") from None
        if not math.isfinite(weight) or weight < 0 or weight > 100:
            raise ValueError("Invalid holding weight")
        identity = str(row.get("isin") or row.get("securityCusip") or "")
        if asset in seen:
            raise ValueError("Duplicate/ambiguous holding symbol")
        seen.add(asset)
        # Cash/derivatives and exchange-suffixed listings are not silently
        # mapped onto US equity sessions. Retain them in excluded evidence.
        if not identity or not re.fullmatch(r"[A-Z][A-Z0-9-]{0,14}",asset):
            excluded.append({"asset":asset,"weight_pct":weight,"reason":"cash_or_unresolved_listing"})
            continue
        members.append({"ticker":asset,"security_id":identity,"weight_pct":weight})
    if not members:
        raise ValueError("No resolvable equity holdings")
    total=sum(m["weight_pct"] for m in members)+sum(x["weight_pct"] for x in excluded)
    if not 95 <= total <= 105:
        raise ValueError("Holding weights suggest partial or inconsistent response")
    return {"schema_version":"rotation.holdings-observation.v1","etf":symbol,
            "source":"FMP /stable/etf/holdings","captured_at":captured.isoformat(),
            "provider_updated_at_raw":sorted({str(r.get("updatedAt") or "") for r in rows}),
            "holdings_effective_at":None,"point_in_time":False,
            "status":"OBSERVATION_ONLY_NO_PROVIDER_DATE",
            "response_sha256":hashlib.sha256(encoded(rows)).hexdigest(),
            "members":members,"excluded":excluded,"reported_weight_pct":total}


def observation_breadth(observation, frames, sessions):
    """Evaluate member MA20 on a completed price date, not on a claimed holding date."""
    if observation.get("status") != "OBSERVATION_ONLY_NO_PROVIDER_DATE":
        raise ValueError("Unsupported holdings observation")
    if len(sessions)<20:
        raise ValueError("Need 20 exchange sessions")
    symbols=[m["ticker"] for m in observation["members"]]
    table=clean_table(pd.DataFrame({s:f.adj_close for s,f in frames.items() if s in symbols}),sessions)
    table=table.reindex(columns=symbols).where(lambda f:f>0)
    ma=table.rolling(20,min_periods=20).mean().iloc[-1]
    last=table.iloc[-1]
    valid=last.notna() & ma.notna()
    above=last.gt(ma) & valid
    weights={m["ticker"]:m["weight_pct"] for m in observation["members"]}
    expected=len(symbols); n=int(valid.sum())
    total_weight=sum(weights.values())
    valid_weight=sum(weights[s] for s in symbols if valid[s])
    return {"etf":observation["etf"],"price_session":sessions[-1].date().isoformat(),
            "holdings_captured_at":observation["captured_at"],"holdings_effective_at":None,
            "status":"OBSERVATION_ONLY_NO_PROVIDER_DATE","point_in_time":False,
            "eligible_members":n,"mapped_equity_members":expected,
            "member_coverage":n/expected,"mapped_equity_weight_pct":total_weight,
            "valid_weight_pct":valid_weight,"weight_coverage":valid_weight/total_weight if total_weight else None,
            "above_ma20_pct":100*int(above.sum())/n if n else None,
            "weighted_above_ma20_pct":100*sum(weights[s] for s in symbols if above[s])/valid_weight if valid_weight else None,
            "measurement_complete":n>=5 and n/expected>=.8 and valid_weight>=80,
            "production_eligible":False,
            "note":"当前持仓观测 × 指定收盘日价格；持仓生效日未披露，不进入历史回测、生产广度确认或分数"}
