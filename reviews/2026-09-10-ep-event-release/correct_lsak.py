"""One-time correction of our LSAK message; no model call and no duplicate post."""
import json
from pathlib import Path
import sys

sys.path.insert(0, '/home/projects/quant/ep-event-current')


def main():
    import requests
    from src.alerts.ep_event import EventOutbox, VerifiedNotifier, ai_payload
    from src.breakouts.ep.event_worker import WorkerConfig, private_text
    from src.breakouts.ep.event_workflow import EventReviewStore, reviewed_report
    from src.breakouts.ep.store import EpStore
    from src.utils.io import atomic_save_json
    config = WorkerConfig.model_validate_json(Path('/etc/quant/ep-event-worker.json').read_text())
    outbox = EventOutbox(config.outbox_database)
    with outbox.connect() as db:
        row = db.execute("SELECT * FROM ep_ai_outbox WHERE message_id=? AND state='SENT'",
                         ('1547523863401996331',)).fetchone()
    if row is None or row['ticker'] != 'LSAK':
        raise ValueError('EXACT_OWN_SENT_MESSAGE_REQUIRED')
    report = reviewed_report(EpStore(config.database, read_only=True),
                             EventReviewStore(config.reviews_database), row['request_key'])
    payload, included = ai_payload(report)
    if len(included) != 1 or report['claims'][0]['text'] != 'Lesaka Technologies发布了财年业绩公告':
        raise ValueError('EXPECTED_REVALIDATED_CORRECTION_REQUIRED')
    payload['content'] += '\n更正：已移除未完成财务核验的每股收益与指引表述；本条仅保留公告发布事实的未核准 AI 摘要。'
    marker = Path(config.output_directory) / 'lsak_message_correction.json'
    audit = {'status': 'ATTEMPTED_NO_AUTO_RETRY', 'message_id': row['message_id'],
             'original_payload': json.loads(row['payload']), 'corrected_payload': payload,
             'reason': 'CHINESE_FINANCIAL_SCOPE_BYPASS'}
    with marker.open('x') as output:
        json.dump(audit, output, ensure_ascii=False)
    webhook = private_text(config.webhook_file)
    VerifiedNotifier(webhook, config.expected_channel_id)
    response = requests.patch(webhook + '/messages/' + row['message_id'], json=payload,
                              timeout=15, allow_redirects=False)
    if response.status_code != 200 or str(response.json().get('id')) != row['message_id']:
        raise ValueError('CORRECTION_UNCONFIRMED_DO_NOT_AUTO_RETRY')
    audit['status'] = 'CORRECTED'
    atomic_save_json(audit, marker)
    print(json.dumps({'status': 'CORRECTED', 'message_id': row['message_id'], 'remaining_claims': len(included),
                      'new_messages': 0, 'model_requests': 0}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'FAILED', 'error_type': type(exc).__name__}))
        sys.exit(1)
