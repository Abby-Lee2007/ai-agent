"""Evaluation framework tests."""
from __future__ import annotations

import unittest
from pathlib import Path

from finagent.evaluation import EvaluationRunner, load_cases


class TestEvaluationRunner(unittest.TestCase):
    def test_bundled_evaluation_cases_pass(self):
        root = Path(__file__).resolve().parents[1]
        report = EvaluationRunner(
            load_cases(root / "evals" / "agent_cases.json")
        ).run()

        self.assertGreater(report["total"], 10)
        self.assertEqual(report["failed"], 0, report)
        self.assertEqual(report["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
