from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from src.breakouts.ep.event_worker import WorkerConfig


@pytest.mark.parametrize('at,collect,execute,skip', [
    ('2026-09-12T08:00:00+00:00', True, True, True),
    ('2026-09-07T12:15:00+00:00', True, True, True),
    ('2026-09-11T20:00:00+00:00', True, True, True),
    ('2026-09-11T07:59:00+00:00', True, True, True),
    ('2026-11-27T18:00:00+00:00', True, True, True),
    ('2026-09-11T12:15:00+00:00', True, True, False),
    ('2026-09-12T08:00:00+00:00', False, True, False),
    ('2026-09-12T08:00:00+00:00', True, False, False),
])
def test_scheduled_run_checks_calendar_before_database_or_network(tmp_path, monkeypatch, capsys,
                                                                 at, collect, execute, skip):
    spec = importlib.util.spec_from_file_location('ep_guard_cli',
        Path(__file__).resolve().parents[1] / 'scripts/run_ep_event_worker.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(at)
    monkeypatch.setattr(cli, 'datetime', Clock)
    def database_boundary(*args, **kwargs):
        raise RuntimeError('DATABASE_BOUNDARY_REACHED')
    monkeypatch.setattr(cli, 'EpStore', database_boundary)
    config = WorkerConfig(database=str(tmp_path / 'missing.db'), reviews_database=str(tmp_path / 'reviews.db'),
        output_directory=str(tmp_path / 'reports'), key_file=str(tmp_path / 'never-read.key'),
        enabled=True, collect_enabled=collect)
    path = tmp_path / 'config.json'
    path.write_text(config.model_dump_json())
    argv = ['worker', '--config', str(path), 'run'] + (['--execute'] if execute else [])
    monkeypatch.setattr(sys, 'argv', argv)
    if skip:
        assert cli.main() == 0
        result = json.loads(capsys.readouterr().out)
        assert result == {'status': 'OUTSIDE_MARKET_WINDOW', 'external_requests': 0,
                          'llm_requests': 0, 'discord_messages': 0}
        assert list(tmp_path.iterdir()) == [path]
    else:
        with pytest.raises(RuntimeError, match='DATABASE_BOUNDARY_REACHED'):
            cli.main()
