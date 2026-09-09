from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import run_mvp


def _args(**updates):
    values = {
        "update": True,
        "no_web": False,
        "serve_only": False,
        "universe": None,
        "only_universe": "SP500",
        "host": None,
        "port": None,
    }
    values.update(updates)
    return SimpleNamespace(**values)


class RunMvpCliTests(unittest.TestCase):
    def test_update_recomputes_published_data_and_returns_nonzero_on_failure(self):
        with (
            patch.object(run_mvp, "parse_args", return_value=_args()),
            patch.object(run_mvp, "run_pipeline", return_value=["SP500"]) as pipeline,
            patch.object(run_mvp, "serve_web") as serve,
        ):
            result = run_mvp.main()

        self.assertEqual(result, 1)
        pipeline.assert_called_once_with(
            universe_limit=None,
            only_universe="SP500",
        )
        serve.assert_not_called()

    def test_successful_update_does_not_start_a_second_web_process(self):
        with (
            patch.object(run_mvp, "parse_args", return_value=_args()),
            patch.object(run_mvp, "run_pipeline", return_value=[]),
            patch.object(run_mvp, "serve_web") as serve,
        ):
            result = run_mvp.main()

        self.assertEqual(result, 0)
        serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
