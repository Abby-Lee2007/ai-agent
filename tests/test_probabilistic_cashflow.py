"""Independent checks for seeded probabilistic cash-flow statistics."""
from __future__ import annotations

from datetime import date, timedelta
import unittest

from finagent.analytics.probabilistic_cashflow import (
    simulate_cashflow_paths,
    summarize_probability_paths,
)
from tests.support import make_agent


def make_series(values):
    start = date(2026, 10, 1)
    return [
        {"date": (start + timedelta(days=index)).isoformat(), "balance": value}
        for index, value in enumerate(values)
    ]


class TestProbabilitySummaries(unittest.TestCase):
    def test_terminal_and_path_minimum_are_separate_known_distributions(self):
        paths = [
            [-10, -5],
            [0, 10],
            [5, 20],
            [100, 90],
            [-20, 30],
        ]
        summary = summarize_probability_paths(
            paths,
            [date(2026, 10, 1), date(2026, 10, 2)],
            100,
        )

        self.assertEqual(summary["terminal_balance"]["p50"], 20.0)
        self.assertEqual(summary["minimum_balance"]["p50"], 0.0)
        self.assertEqual(
            summary["negative_balance"]["probability_negative_end"],
            0.2,
        )
        self.assertEqual(
            summary["negative_balance"]["probability_any_negative_during_period"],
            0.4,
        )
        self.assertEqual(
            summary["negative_balance"]["daily_probability"][0]["probability"],
            0.4,
        )
        self.assertEqual(
            summary["negative_balance"]["daily_probability"][1]["probability"],
            0.2,
        )
        self.assertEqual(summary["worst_tail"]["path_count"], 1)
        self.assertEqual(
            summary["worst_liquidity_tail"]["mean_min_balance"],
            -20.0,
        )
        self.assertEqual(
            summary["worst_liquidity_tail"]["cvar_5_terminal_loss"],
            70.0,
        )
        self.assertEqual(summary["worst_tail"]["cvar_5_terminal_loss"], 105.0)

    def test_same_seed_and_parameters_reproduce_all_distributions(self):
        series = make_series([10_000, 9_950, 9_900, 9_850, 9_800])
        first = simulate_cashflow_paths(
            series,
            10_000,
            variable_daily_mean=50,
            variable_daily_std=15,
            salary_by_day=[0, 0, 0, 0, 0],
            simulations=250,
            seed=12345,
        )
        second = simulate_cashflow_paths(
            series,
            10_000,
            variable_daily_mean=50,
            variable_daily_std=15,
            salary_by_day=[0, 0, 0, 0, 0],
            simulations=250,
            seed=12345,
        )

        self.assertEqual(first, second)
        self.assertEqual(first["simulations"], 250)
        self.assertEqual(first["random_seed"], 12345)
        self.assertEqual(first["input_fingerprint"], second["input_fingerprint"])

    def test_higher_variable_expense_dispersion_widens_lower_tail(self):
        series = make_series([10_000] * 30)
        low = simulate_cashflow_paths(
            series,
            10_000,
            variable_daily_mean=100,
            variable_daily_std=5,
            simulations=800,
            seed=42,
        )
        high = simulate_cashflow_paths(
            series,
            10_000,
            variable_daily_mean=100,
            variable_daily_std=60,
            simulations=800,
            seed=42,
        )
        low_tail_width = (
            low["minimum_balance"]["p50"] - low["minimum_balance"]["p05"]
        )
        high_tail_width = (
            high["minimum_balance"]["p50"] - high["minimum_balance"]["p05"]
        )

        self.assertGreater(high_tail_width, low_tail_width)
        self.assertLess(
            high["minimum_balance"]["p05"],
            high["minimum_balance"]["p50"],
        )

    def test_zero_dispersion_produces_cash_only_deterministic_paths(self):
        series = make_series([1_000, 1_050, 1_100])
        summary = simulate_cashflow_paths(
            series,
            1_000,
            variable_daily_mean=0,
            variable_daily_std=0,
            simulations=100,
            seed=7,
        )
        self.assertEqual(
            summary["terminal_balance"]["p05"],
            summary["terminal_balance"]["p95"],
        )
        self.assertEqual(
            summary["minimum_balance"]["p05"],
            summary["minimum_balance"]["p95"],
        )

    def test_invalid_paths_and_non_finite_values_are_rejected(self):
        with self.assertRaises(ValueError):
            summarize_probability_paths(
                [[1, 2], [3]],
                [date(2026, 10, 1), date(2026, 10, 2)],
                0,
            )
        with self.assertRaises(ValueError):
            summarize_probability_paths(
                [[1, float("nan")]],
                [date(2026, 10, 1), date(2026, 10, 2)],
                0,
            )

    def test_forecast_tool_is_repeatable_and_exposes_both_distributions(self):
        agent = make_agent()
        tool = agent.registry.get("forecast.cashflow")
        context = agent.context()
        first = tool.run(context, months=3)
        second = tool.run(context, months=3)
        probability = first.data["forecast"]["probability"]

        self.assertEqual(probability, second.data["forecast"]["probability"])
        self.assertTrue(probability["daily_balance_quantiles"])
        self.assertNotEqual(
            probability["terminal_balance"]["p50"],
            probability["minimum_balance"]["p50"],
        )
        self.assertLessEqual(
            probability["terminal_balance"]["p05"],
            probability["terminal_balance"]["p25"],
        )
        self.assertLessEqual(
            probability["terminal_balance"]["p25"],
            probability["terminal_balance"]["p50"],
        )
        self.assertLessEqual(
            probability["terminal_balance"]["p50"],
            probability["terminal_balance"]["p95"],
        )


if __name__ == "__main__":
    unittest.main()
