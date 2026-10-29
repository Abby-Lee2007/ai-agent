"""P2 fairness, stability, and ablation audit tests."""
from __future__ import annotations

import unittest

from finagent.data.providers import build_data_providers
from finagent.evaluation.model_audit import run_model_audit
from tests.support import make_store


class TestModelAudit(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.providers = build_data_providers(self.store)

    def _run(self):
        return run_model_audit(
            self.store,
            self.providers,
            self.store.clock,
            "u_001",
            simulations=80,
        )

    def test_audit_has_required_sections_and_metrics(self):
        report = self._run()
        self.assertEqual(
            set(report),
            {"metadata", "fairness", "stability", "ablation"},
        )
        self.assertIn("health_score", report["fairness"]["metrics"])
        self.assertIn("risk_level", report["fairness"]["metrics"])
        self.assertIn("equity_share", report["fairness"]["metrics"])
        self.assertIn("risk_asset_share", report["fairness"]["metrics"])
        self.assertIn("max_health_score_delta", report["stability"]["metrics"])
        self.assertIn("health_dimension", report["ablation"])
        self.assertIn("risk_dimension", report["ablation"])
        self.assertIn("recommendation", report["ablation"])
        self.assertIn(
            "risk_aversion_delta",
            report["ablation"]["recommendation"][1],
        )

    def test_fairness_is_labelled_as_synthetic_stress_test(self):
        report = self._run()
        self.assertIn("synthetic", report["fairness"]["disclaimer"].lower())

    def test_audit_is_deterministic(self):
        first = self._run()
        second = self._run()
        self.assertEqual(first, second)

    def test_personas_cover_age_income_and_risk_groups(self):
        report = self._run()
        age_groups = {
            row["age_band"]
            for row in report["fairness"]["personas"]
        }
        income_groups = {
            row["income_band"]
            for row in report["fairness"]["personas"]
        }
        self.assertGreaterEqual(len(age_groups), 3)
        self.assertGreaterEqual(len(income_groups), 3)


if __name__ == "__main__":
    unittest.main()
