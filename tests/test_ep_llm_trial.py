import importlib.util
import json
from pathlib import Path
import sys

import pytest

from test_ep_llm import seeded
from test_ep_sources import observed


@pytest.fixture
def trial():
    path = Path(__file__).resolve().parents[1] / "reviews/2026-09-09-ep-llm-trial/run.py"
    spec = importlib.util.spec_from_file_location("ep_trial", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_private_key_configure_and_no_overwrite(trial, tmp_path, monkeypatch):
    path = tmp_path / "key"
    key = "sk-test-" + "x" * 32
    monkeypatch.setattr(trial.getpass, "getpass", lambda _: key)
    result = trial.configure_key(path)
    assert key not in json.dumps(result)
    assert path.stat().st_mode & 0o777 == 0o600
    assert trial.read_key(path) == key
    with pytest.raises(ValueError, match="ALREADY_EXISTS"):
        trial.configure_key(path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="MODE_0600"):
        trial.read_key(path)


def test_symlink_key_rejected(trial, tmp_path):
    target = tmp_path / "target"
    target.write_text("sk-test-" + "x" * 32)
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError):
        trial.read_key(link)
    with pytest.raises(ValueError, match="ALREADY_EXISTS"):
        trial.configure_key(link)


def test_plan_never_reads_key_or_honors_ambient_override(trial, observed, monkeypatch, capsys):
    store, _, sid = seeded(observed)
    monkeypatch.setattr(trial, "DATABASE", store.path)
    monkeypatch.setattr(trial, "SOURCES", {"SNOW": sid})
    monkeypatch.setattr(trial, "read_key", lambda _: pytest.fail("plan read key"))
    monkeypatch.setenv("EP_LLM_MODEL", "gpt-5.4")
    monkeypatch.setenv("EP_LLM_ENABLED", "true")
    monkeypatch.setenv("EP_LLM_TOTAL_MICROUSD", "999999999")
    monkeypatch.setattr(sys, "argv", ["run.py", "plan"])
    assert trial.main() == 0
    value = json.loads(capsys.readouterr().out)
    assert value["model"] == "gpt-5.4-mini"
    assert value["total_limit_microusd"] == 10_000_000
    assert value["http_requests"] == 0
    assert value["budget"]["calls"] == 0
    assert value["sources"]["SNOW"]["enabled"] is False


def test_run_requires_execute_before_key_or_write(trial, observed, monkeypatch):
    store, _, sid = seeded(observed)
    monkeypatch.setattr(trial, "DATABASE", store.path)
    monkeypatch.setattr(trial, "SOURCES", {"SNOW": sid})
    monkeypatch.setattr(trial, "read_key", lambda _: pytest.fail("missing execute read key"))
    monkeypatch.setattr(sys, "argv", ["run.py", "run", "SNOW"])
    with pytest.raises(ValueError, match="EXPLICIT_EXECUTE_REQUIRED"):
        trial.main()
    assert trial.budget_status(store)["calls"] == 0


def test_kimi_trial_plan_uses_same_database_and_fixed_budget(trial, observed, monkeypatch, capsys):
    store, _, sid = seeded(observed)
    monkeypatch.setattr(trial, "DATABASE", store.path)
    monkeypatch.setattr(trial, "SOURCES", {"SNOW": sid})
    monkeypatch.setattr(trial, "read_key", lambda _: pytest.fail("plan read key"))
    monkeypatch.setenv("EP_LLM_TOTAL_MICROUSD", "999999999")
    monkeypatch.setattr(sys, "argv", ["run.py", "--provider", "kimi-cn", "plan"])
    assert trial.main() == 0
    value = json.loads(capsys.readouterr().out)
    assert value["model"] == "kimi-k2.6" and value["provider"] == "kimi-cn"
    assert value["database"] == str(store.path)
    assert value["total_limit_microusd"] == 10_000_000 and value["http_requests"] == 0


def test_paid_trial_requires_explicit_provider(trial, monkeypatch):
    monkeypatch.setattr(trial, "read_key", lambda _: pytest.fail("unspecified provider read key"))
    monkeypatch.setattr(sys, "argv", ["run.py", "run", "GTLB", "--execute"])
    with pytest.raises(ValueError, match="EXPLICIT_PROVIDER_REQUIRED"):
        trial.main()
