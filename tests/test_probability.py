"""Probabilistic cash-flow forecast tests."""
from __future__ import annotations

import unittest

from finagent.analytics.probability import build_probability_forecast
from finagent.data.providers import build_data_providers
from tests.support import make_store


class TestProbabilityForecast(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.providers = build_data_providers(self.store)

    def _build(self, seed: int = 20261028):
        return build_probability_forecast(
            self.store,
            self.providers,
            self.store.clock,
            "u_001",
            months=3,
            simulations=120,
            seed=seed,
        )

    def test_quantiles_are_monotonic_and_seeded(self):
        first = self._build()
        second = self._build()
        self.assertEqual(first["terminal_quantiles"], second["terminal_quantiles"])
        quantiles = first["terminal_quantiles"]
        self.assertLessEqual(quantiles["p05"], quantiles["p25"])
        self.assertLessEqual(quantiles["p25"], quantiles["p50"])
        self.assertLessEqual(quantiles["p50"], quantiles["p75"])
        self.assertLessEqual(quantiles["p75"], quantiles["p95"])

    def test_daily_bands_cover_the_whole_horizon(self):
        result = self._build()
        self.assertEqual(
            len(result["daily_quantiles"]),
            result["baseline"]["horizon_days"] + 1,
        )
        first = result["daily_quantiles"][0]
        last = result["daily_quantiles"][-1]
        for key in ("p05", "p25", "p50", "p75", "p95"):
            self.assertIn(key, first)
            self.assertIn(key, last)

    def test_probability_fields_are_present(self):
        result = self._build()
        self.assertEqual(result["scenario_count"], 120)
        self.assertEqual(result["random_seed"], 20261028)
        self.assertGreaterEqual(result["negative_probability"], 0.0)
        self.assertLessEqual(result["negative_probability"], 1.0)
        self.assertIn("mean_terminal_balance", result["worst_5pct"])
        self.assertIn("mean_min_balance", result["worst_5pct"])

    def test_different_seed_changes_empirical_paths(self):
        first = self._build(seed=1)
        second = self._build(seed=2)
        self.assertNotEqual(first["terminal_quantiles"], second["terminal_quantiles"])


if __name__ == "__main__":
    unittest.main()
