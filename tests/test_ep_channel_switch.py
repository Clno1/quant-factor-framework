import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from src.breakouts.ep.event_worker import WorkerConfig


def module():
    path = Path(__file__).resolve().parents[1] / 'scripts/configure_ep_event_discord.py'
    spec = importlib.util.spec_from_file_location('configure_ep_route', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize('channel,expected,success', [('456', '456', True), ('123', '123', False), ('456', '789', False)])
def test_channel_switch_keeps_old_key_and_checks_expected_destination(tmp_path, monkeypatch, capsys, channel, expected, success):
    key = tmp_path / 'old.key'
    key.write_text('old-secret')
    key.chmod(0o600)
    cfg = WorkerConfig(database=str(tmp_path / 'ledger.db'), reviews_database=str(tmp_path / 'reviews.db'),
        output_directory=str(tmp_path / 'reports'), key_file=str(tmp_path / 'model.key'),
        webhook_file=str(key), expected_channel_id='123', outbox_database=str(tmp_path / 'outbox.db'), delivery_enabled=False)
    config = tmp_path / 'config.json'
    config.write_text(cfg.model_dump_json())
    before = config.read_bytes()
    mod = module()
    monkeypatch.setattr(sys, 'argv', ['configure', '--config', str(config), '--replace', '--channel-id', expected])
    monkeypatch.setattr(mod.getpass, 'getpass', lambda _: 'new-secret')
    monkeypatch.setattr(mod, 'webhook_channel', lambda _: channel)
    if success:
        mod.main()
        updated = json.loads(config.read_text())
        assert updated['expected_channel_id'] == '456' and updated['delivery_enabled']
        assert Path(updated['webhook_file']).read_text().strip() == 'new-secret'
        assert os.stat(updated['webhook_file']).st_mode & 0o777 == 0o600
        assert 'secret' not in capsys.readouterr().out
    else:
        with pytest.raises(ValueError):
            mod.main()
        assert config.read_bytes() == before
    assert key.read_text() == 'old-secret'
