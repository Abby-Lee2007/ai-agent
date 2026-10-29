"""Independent hard-constraint and sensitivity tests for portfolio optimization."""
from __future__ import annotations

import copy
import unittest

from finagent.analytics.portfolio_optimizer import (
    optimize_within_constraints,
    verify_portfolio_solution,
)
from finagent.analytics.product_risk import portfolio_risk_summary
from finagent.data.models import WealthProduct


def candidates():
    return [
        {
            "product_id": "CASH_NEAR",
            "name": "现金管理",
            "risk_level": 1,
            "expected_return_pct": 2.0,
            "volatility_pct": 0.5,
            "max_drawdown_pct": 0.5,
            "fee_rate_pct": 0.1,
            "term_days": 0,
            "min_amount": 1,
            "asset_class": "fund",
        },
        {
            "product_id": "BOND",
            "name": "债券组合",
            "risk_level": 2,
            "expected_return_pct": 4.0,
            "volatility_pct": 5.0,
            "max_drawdown_pct": 4.0,
            "fee_rate_pct": 0.15,
            "term_days": 30,
            "min_amount": 100,
            "asset_class": "bond",
        },
        {
            "product_id": "EQUITY",
            "name": "权益组合",
            "risk_level": 4,
            "expected_return_pct": 10.0,
            "volatility_pct": 25.0,
            "max_drawdown_pct": 30.0,
            "fee_rate_pct": 0.5,
            "term_days": 0,
            "min_amount": 100,
            "asset_class": "stock",
        },
    ]


class TestPortfolioOptimizer(unittest.TestCase):
    def test_solution_passes_independent_constraint_verification(self):
        result = optimize_within_constraints(
            candidates(),
            100_000,
            max_risk_level=3,
            horizon_days=365,
            drawdown_tolerance_pct=20,
            cash_buffer_weight=0.10,
            risk_aversion=0.6,
            max_single_weight=0.35,
            max_portfolio_cvar_5_pct=5,
        )

        self.assertEqual(result["status"], "optimized")
        self.assertTrue(result["independent_verification"]["passed"])
        self.assertAlmostEqual(
            sum(item["weight"] for item in result["allocations"]),
            1.0,
        )
        self.assertGreaterEqual(
            sum(
                item["weight"] for item in result["allocations"]
                if item["product_id"] == "CASH"
            ),
            0.10,
        )
        self.assertTrue(all(
            item["weight"] <= 0.35 + 1e-8
            for item in result["allocations"]
            if item["product_id"] != "CASH"
        ))

    def test_higher_risk_aversion_does_not_increase_cvar(self):
        low_aversion = optimize_within_constraints(
            candidates(),
            100_000,
            max_risk_level=4,
            horizon_days=365,
            drawdown_tolerance_pct=40,
            cash_buffer_weight=0.05,
            risk_aversion=0.0,
            max_single_weight=0.40,
            max_portfolio_cvar_5_pct=100,
        )
        high_aversion = optimize_within_constraints(
            candidates(),
            100_000,
            max_risk_level=4,
            horizon_days=365,
            drawdown_tolerance_pct=40,
            cash_buffer_weight=0.05,
            risk_aversion=3.0,
            max_single_weight=0.40,
            max_portfolio_cvar_5_pct=100,
        )

        self.assertLessEqual(
            high_aversion["metrics"]["cvar_5_loss_pct"],
            low_aversion["metrics"]["cvar_5_loss_pct"] + 1e-8,
        )

    def test_cvar_penalty_ablation_changes_only_the_penalty_switch(self):
        with_penalty = optimize_within_constraints(
            candidates(),
            100_000,
            max_risk_level=4,
            horizon_days=365,
            drawdown_tolerance_pct=40,
            cash_buffer_weight=0.0,
            risk_aversion=2.0,
            max_single_weight=0.40,
            max_portfolio_cvar_5_pct=100,
            include_cvar_penalty=True,
        )
        without_penalty = optimize_within_constraints(
            candidates(),
            100_000,
            max_risk_level=4,
            horizon_days=365,
            drawdown_tolerance_pct=40,
            cash_buffer_weight=0.0,
            risk_aversion=2.0,
            max_single_weight=0.40,
            max_portfolio_cvar_5_pct=100,
            include_cvar_penalty=False,
        )

        self.assertTrue(with_penalty["objective"]["include_cvar_penalty"])
        self.assertFalse(without_penalty["objective"]["include_cvar_penalty"])
        self.assertLessEqual(
            with_penalty["metrics"]["cvar_5_loss_pct"],
            without_penalty["metrics"]["cvar_5_loss_pct"] + 1e-8,
        )

    def test_infeasible_constraints_return_cash_and_do_not_relax(self):
        result = optimize_within_constraints(
            [
                candidate for candidate in candidates()
                if candidate["risk_level"] > 1
            ],
            5_000,
            max_risk_level=1,
            horizon_days=30,
            drawdown_tolerance_pct=1,
            cash_buffer_weight=0.10,
            max_portfolio_cvar_5_pct=1,
        )

        self.assertEqual(result["status"], "cash_only")
        self.assertEqual(result["allocations"][0]["product_id"], "CASH")
        self.assertEqual(result["allocations"][0]["weight"], 1.0)
        self.assertEqual(
            result["constraints"]["max_risk_level"],
            1,
        )
        self.assertTrue(result["independent_verification"]["passed"])

    def test_tampered_solution_fails_external_verifier(self):
        result = optimize_within_constraints(
            candidates(),
            100_000,
            max_risk_level=3,
            horizon_days=365,
            drawdown_tolerance_pct=20,
            cash_buffer_weight=0.10,
            max_single_weight=0.35,
            max_portfolio_cvar_5_pct=5,
        )
        tampered = copy.deepcopy(result)
        tampered["allocations"][0]["weight"] = 0.90
        verification = verify_portfolio_solution(
            tampered,
            candidates(),
            result["constraints"],
            100_000,
        )

        self.assertFalse(verification["passed"])

    def test_portfolio_volatility_uses_weights_not_simple_product_average(self):
        products = [
            WealthProduct(
                "LOW",
                "低波",
                "债券基金",
                2,
                0.04,
                asset_class="bond",
            ),
            WealthProduct(
                "HIGH",
                "高波",
                "股票基金",
                4,
                0.10,
                asset_class="stock",
            ),
        ]
        summary = portfolio_risk_summary(products, [1.0, 0.0])

        self.assertAlmostEqual(summary["weighted_volatility_pct"], 4.0)
        self.assertNotEqual(
            summary["weighted_volatility_pct"],
            summary["average_volatility_pct"],
        )


if __name__ == "__main__":
    unittest.main()
