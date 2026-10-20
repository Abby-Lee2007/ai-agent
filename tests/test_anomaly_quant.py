"""异常识别的量化指标测试。"""
from __future__ import annotations

import unittest

from scripts.demo_anomaly_case import build_case
from finagent.risk.engine import RiskEngine


class TestQuantifiedAnomaly(unittest.TestCase):
    def setUp(self):
        self.store = build_case()
        self.engine = RiskEngine(self.store, self.store.clock)
        self.items = self.engine.detect_anomalies("u_001", include_subscriptions=False)

    def test_night_large_has_score_and_metrics(self):
        night = next(a for a in self.items if a.kind == "night_large")
        self.assertGreaterEqual(night.score, 70)
        self.assertIn("hour", night.metrics)
        self.assertEqual(night.metrics["hour"], 3)

    def test_amount_outlier_reports_zscore(self):
        outlier = next(a for a in self.items if a.kind == "amount_outlier")
        self.assertGreater(outlier.metrics["zscore"], 3)
        self.assertGreater(outlier.metrics["category_mean"], 0)

    def test_new_merchant_large_is_flagged(self):
        kinds = {a.kind for a in self.items}
        self.assertIn("new_merchant_large", kinds)

    def test_results_sorted_by_severity(self):
        self.assertEqual(self.items[0].level, "danger")


if __name__ == "__main__":
    unittest.main()
