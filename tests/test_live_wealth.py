"""Tests for public-market quantitative recommendation analytics."""
from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from finagent.analytics.live_wealth import (
    calculate_live_metrics,
    diversified_selection,
    score_live_product,
)
from finagent.data.models import WealthProduct


def make_history(observations: int = 200) -> dict:
    points = []
    for index in range(observations):
        close = 100 * (1 + index * 0.0007) + math.sin(index / 7) * 2
        points.append({
            "date": f"2026-{(index // 28) % 12 + 1:02d}-{index % 28 + 1:02d}",
            "close": round(close, 4),
        })
    return {
        "symbol": "SH600000",
        "source": "东方财富",
        "points": points,
        "is_demo": False,
    }


def make_product(risk_level: int = 3) -> WealthProduct:
    return WealthProduct(
        product_id=f"P{risk_level}",
        name=f"测试产品{risk_level}",
        category="股票",
        risk_level=risk_level,
        expected_return=0.0,
        min_amount=100.0,
        liquidity="T+1",
        symbol="sh600000",
        symbol_type="stock",
        asset_class="stock",
        is_live=True,
        is_demo=False,
    )


class TestLiveMetrics(unittest.TestCase):
    def test_metrics_include_return_volatility_drawdown_and_trend(self):
        metrics = calculate_live_metrics(
            make_history(),
            risk_free_rate_pct=1.4,
        )
        self.assertTrue(metrics["available"])
        self.assertEqual(metrics["observations"], 200)
        self.assertIn("annualized_return_pct", metrics)
        self.assertGreater(metrics["annualized_volatility_pct"], 0)
        self.assertGreaterEqual(metrics["max_drawdown_pct"], 0)
        self.assertIn("sharpe_ratio", metrics)
        self.assertIn("ma20", metrics)

    def test_insufficient_window_is_not_reported_as_zero_return(self):
        metrics = calculate_live_metrics(make_history(40))
        self.assertIsNotNone(metrics["return_1m_pct"])
        self.assertIsNone(metrics["return_3m_pct"])
        self.assertIsNone(metrics["return_6m_pct"])
        self.assertIsNone(metrics["return_1y_pct"])

    def test_short_horizon_penalizes_high_drawdown_asset(self):
        metrics = calculate_live_metrics(make_history())
        metrics["max_drawdown_pct"] = 25.0
        metrics["annualized_volatility_pct"] = 30.0
        result = score_live_product(
            make_product(4),
            metrics,
            user_risk_level=3,
            drawdown_tolerance_pct=8.0,
            horizon="short",
            quote=SimpleNamespace(pe=50.0, pb=4.0),
        )
        self.assertLess(result["score"], 60)
        self.assertTrue(any("短期" in reason for reason in result["reasons"]))

    def test_matching_risk_beats_mismatched_risk_with_same_history(self):
        metrics = calculate_live_metrics(make_history())
        matched = score_live_product(
            make_product(3),
            metrics,
            user_risk_level=3,
            drawdown_tolerance_pct=20.0,
        )
        mismatched = score_live_product(
            make_product(5),
            metrics,
            user_risk_level=2,
            drawdown_tolerance_pct=10.0,
        )
        self.assertGreater(matched["score"], mismatched["score"])

    def test_diversified_selection_limits_same_category_concentration(self):
        ranked = [
            {"product_id": f"S{i}", "category": "股票", "score": 90 - i}
            for i in range(5)
        ] + [
            {"product_id": "B1", "category": "债券", "score": 70},
            {"product_id": "F1", "category": "基金", "score": 65},
        ]
        selected = diversified_selection(ranked, limit=4)
        stock_count = sum(1 for item in selected if item["category"] == "股票")
        self.assertEqual(len(selected), 4)
        self.assertLessEqual(stock_count, 2)


if __name__ == "__main__":
    unittest.main()
