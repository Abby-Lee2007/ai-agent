"""Constrained portfolio optimizer tests."""
from __future__ import annotations

import unittest

from finagent.analytics.optimizer import optimize_portfolio
from finagent.data.providers import build_data_providers
from tests.support import make_store


class TestPortfolioOptimizer(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.providers = build_data_providers(self.store)
        self.products = [
            product
            for product in self.providers.wealth.products()
            if product.risk_level <= 3 and product.min_amount <= 10000
        ][:6]

    def _optimize(self, **overrides):
        params = {
            "products": self.products,
            "liquid_assets": 100000.0,
            "requested_investment": 50000.0,
            "client_risk": 3,
            "horizon_days": 365,
            "cash_buffer": 30000.0,
            "drawdown_limit": 0.25,
            "seed": 7,
        }
        params.update(overrides)
        return optimize_portfolio(**params)

    def test_optimizer_respects_risk_and_weight_caps(self):
        result = self._optimize()
        self.assertIn(result["status"], {"optimized", "cash_only"})
        self.assertAlmostEqual(sum(result["weights"].values()), 1.0, places=6)
        product_allocations = [
            item for item in result["allocations"] if item["product_id"] != "cash"
        ]
        for item in product_allocations:
            self.assertLessEqual(item["risk_level"], 3)
            self.assertLessEqual(item["weight"], 0.60001)

    def test_cash_buffer_is_never_consumed(self):
        result = self._optimize(liquid_assets=40000.0, cash_buffer=30000.0)
        self.assertLessEqual(result["available_budget"], 10000.0)
        self.assertTrue(result["constraints"]["cash_buffer_satisfied"])

    def test_infeasible_buffer_returns_cash_safe_fallback(self):
        result = self._optimize(liquid_assets=20000.0, cash_buffer=30000.0)
        self.assertEqual(result["status"], "no_feasible_portfolio")
        self.assertEqual(result["weights"], {"cash": 1.0})
        self.assertFalse(result["constraints"]["cash_buffer_satisfied"])

    def test_result_is_deterministic(self):
        first = self._optimize()
        second = self._optimize()
        self.assertEqual(first, second)

    def test_optimizer_reports_objective_components(self):
        result = self._optimize()
        for key in (
            "expected_return",
            "volatility",
            "cvar_5pct",
            "estimated_drawdown",
            "objective_score",
        ):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
