"""Bounded SG acceptance operations; emits no credentials or webhook URLs."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path('/home/projects/quant/ep-event-current')
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['ingest', 'activate', 'summary', 'delivery-test'])
    args = parser.parse_args()
    from src.breakouts.ep.event_worker import WorkerConfig, budget, private_text
    from src.breakouts.ep.store import EpStore
    from src.utils.io import atomic_save_json
    path = Path('/etc/quant/ep-event-worker.json')
    config = WorkerConfig.model_validate_json(path.read_text())
    if args.command == 'ingest':
        from dotenv import load_dotenv
        from src.breakouts.ep.event_ingest import ingest
        load_dotenv('/etc/quant/market-data.env', override=True)
        load_dotenv('/etc/quant/ep-event-worker.env', override=True)
        result = ingest(config)
        atomic_save_json(result, Path(config.output_directory) / 'ingest_acceptance.json')
    elif args.command == 'activate':
        backup = path.with_suffix('.archive-acceptance.json')
        if backup.exists():
            raise ValueError('CONFIG_BACKUP_ALREADY_EXISTS')
        atomic_save_json(config.model_dump(), backup)
        updated = config.model_dump()
        updated.update(jobs=[], collect_enabled=True)
        WorkerConfig.model_validate(updated)
        atomic_save_json(updated, path)
        result = {'live_collection_configured': True, 'timer_started': False}
    elif args.command == 'delivery-test':
        from src.alerts.ep_event import VerifiedNotifier
        marker = Path(config.output_directory) / 'deployment_message.json'
        if marker.exists():
            raise ValueError('DEPLOYMENT_MESSAGE_ALREADY_ATTEMPTED')
        atomic_save_json({'status': 'ATTEMPTED_NO_AUTO_RETRY'}, marker)
        sender = VerifiedNotifier(private_text(config.webhook_file), config.expected_channel_id)
        result = sender.send({'content': 'EP 部署通道测试：这里是动量提醒频道。后续公告解读将明确标注“未核准的 AI 解读”，不代表评级、突破确认或买入信号。本条仅验证发送链路，不包含市场信号。',
                              'allowed_mentions': {'parse': []}})
        atomic_save_json({'status': 'SENT', 'message_id': result['message_id']}, marker)
    else:
        result = {'budget': budget(EpStore(config.database, read_only=True)),
                  'collect_enabled': config.collect_enabled, 'delivery_enabled': config.delivery_enabled}
        latest = Path(config.output_directory) / 'latest.json'
        if latest.exists():
            data = json.loads(latest.read_text())
            result['latest'] = {k: data.get(k) for k in ('started_at', 'status', 'external_requests', 'ingestion', 'notifications', 'selection')}
            result['items'] = [{'source_id': i['source_id'], 'status': i.get('status'),
                                'ticker': i.get('report', {}).get('ticker'),
                                'accepted': len(i.get('report', {}).get('claims', [])),
                                'rejected': len(i.get('report', {}).get('rejected', []))} for i in data['items']]
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error_type': type(exc).__name__}))
        sys.exit(1)
