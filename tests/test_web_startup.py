from __future__ import annotations

from unittest.mock import Mock, patch

from src.webapp.app import _recover_application_state


def test_startup_activates_waiting_backtest_reconciliation():
    runner = Mock()
    runner.reconcile_waiting.return_value = 2
    with patch("src.backtest.store.startup_recovery", return_value=1) as recover:
        with patch("src.backtest.runner.get_runner", return_value=runner) as get_runner:
            interrupted, submitted = _recover_application_state()

    assert (interrupted, submitted) == (1, 2)
    recover.assert_called_once_with()
    get_runner.assert_called_once_with()
    runner.reconcile_waiting.assert_called_once_with()
