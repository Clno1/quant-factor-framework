#!/usr/bin/env python3
"""Preview or explicitly send one upgrade acceptance message, outside the daily outbox."""
from pathlib import Path
import argparse
import hashlib
import re
import sys
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))

import pandas as pd
from src.alerts.config import load_local_env
from src.alerts.discord import DiscordDeliveryError, DiscordNotifier, validate_discord_payload
from src.group_analytics.adapters import _atomic_json, _exclusive_file_lock
from src.group_analytics.calendar import latest_completed_session
from src.group_analytics.rotation.replay import replay_snapshot
from src.group_analytics.rotation.store import RotationStore, encoded
from src.premarket_digest.rotation import load_rotation_report, rotation_payload
from src.premarket_digest.settings import load_premarket_digest_settings


def deliver_once(payload, notifier, receipt):
    """Any recorded attempt prevents retries, including uncertain delivery."""
    receipt=Path(receipt)
    payload=validate_discord_payload(payload)
    with _exclusive_file_lock(receipt.with_suffix(".lock")):
        if receipt.exists():
            raise ValueError("Acceptance already attempted; inspect receipt, do not resend")
        state={"status":"ATTEMPTING","started_at":pd.Timestamp.now(tz="UTC").isoformat(),
               "payload_sha256":hashlib.sha256(encoded(payload)).hexdigest()}
        _atomic_json(receipt,state)  # durable before external side effect
        try:
            result=notifier.send(payload)
        except DiscordDeliveryError as exc:
            state.update(status="UNKNOWN" if exc.uncertain else "FAILED",reason=exc.reason)
            _atomic_json(receipt,state)
            raise
        state.update(status="SENT",message_id=result["message_id"],
                     completed_at=pd.Timestamp.now(tz="UTC").isoformat())
        _atomic_json(receipt,state)
        return state


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path)
    parser.add_argument("--acceptance-id",required=True)
    parser.add_argument("--output-root",type=Path)
    parser.add_argument("--send",action="store_true",help="One actual sector-channel message, no role mention")
    args=parser.parse_args(argv)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}",args.acceptance_id):
        raise ValueError("Invalid acceptance id")
    if args.env_file and load_local_env(args.env_file) is None:
        raise ValueError("Missing env file")
    settings=load_premarket_digest_settings(load_env=False)
    store=RotationStore(args.output_root)
    report=load_rotation_report(latest_completed_session().date().isoformat(),store=store,now=pd.Timestamp.now(tz="UTC"))
    if replay_snapshot(report)["status"] != "MATCH":
        raise ValueError("Frozen-input replay failed")
    payload=rotation_payload(report,SimpleNamespace(target_session="升级验收"),settings)
    payload["content"]="板块轮动 V2 升级验收｜中文主站与摘要已接通"
    payload["allowed_mentions"]={"parse":[]}
    payload["embeds"][0]["title"]="板块轮动 V2 · 升级验收（非交易信号）"
    payload["embeds"][0]["description"] += "\n仅验收展示和投递：TradingView外部数值对账待完成；当前验证未证明优于简单动量排名。"
    root=store.root/"acceptance"
    _atomic_json(root/(args.acceptance_id+".preview.json"),payload)
    if not args.send:
        print(encoded({"status":"PREVIEW","run_id":report["run_id"],"payload":payload}).decode())
        return 0
    notifier=DiscordNotifier(settings.sector_rotation_webhook_url,max_rate_limit_retries=0)
    result=deliver_once(payload,notifier,root/(args.acceptance_id+".receipt.json"))
    print(encoded(result).decode())
    return 0


if __name__=="__main__": raise SystemExit(main())
