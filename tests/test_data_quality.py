"""Unified data-quality formula, critical-field gate, and identity tests."""
from __future__ import annotations

import unittest

from finagent.analytics.data_quality import (
    build_data_quality,
    composite_quality_score,
)
from finagent.data.providers import build_data_providers
from tests.support import make_agent, make_store


class TestDataQuality(unittest.TestCase):
    def test_weighted_formula_can_produce_exactly_99(self):
        self.assertEqual(
            composite_quality_score(
                completeness=1.0,
                freshness=0.95,
                anomaly=0.0,
                coverage=1.0,
            ),
            99.0,
        )

    def test_demo_snapshot_is_usable_but_not_labelled_production_quality(self):
        store = make_store()
        quality = build_data_quality(
            store,
            build_data_providers(store),
            store.clock,
            "u_001",
        )

        self.assertEqual(quality["overall"]["score"], 100.0)
        self.assertTrue(quality["overall"]["personalization_allowed"])
        self.assertFalse(quality["is_production_data_quality"])
        self.assertNotIn("ocr", quality)

    def test_missing_critical_transactions_block_execution(self):
        store = make_store()
        store.transactions.clear()
        quality = build_data_quality(
            store,
            build_data_providers(store),
            store.clock,
            "u_001",
        )

        self.assertTrue(quality["overall"]["execution_blocked"])
        self.assertFalse(quality["overall"]["personalization_allowed"])
        self.assertLessEqual(quality["overall"]["score"], 49.0)
        self.assertTrue(any(
            "transactions" in reason
            for reason in quality["block_reasons"]
        ))

    def test_stale_transactions_are_blocked_even_if_fields_are_complete(self):
        store = make_store()
        for txn in store.transactions.values():
            txn.timestamp = "2025-01-01T12:00:00"
        quality = build_data_quality(
            store,
            build_data_providers(store),
            store.clock,
            "u_001",
        )

        transaction_quality = quality["domains"]["transactions"]
        self.assertEqual(transaction_quality["missing_rate"], 0.0)
        self.assertEqual(transaction_quality["freshness"], 0.0)
        self.assertEqual(transaction_quality["status"], "blocked")
        self.assertFalse(quality["overall"]["personalization_allowed"])

    def test_anomaly_and_coverage_metrics_have_explicit_denominators(self):
        store = make_store()
        quality = build_data_quality(
            store,
            build_data_providers(store),
            store.clock,
            "u_001",
        )
        domain = quality["domains"]["transactions"]

        self.assertEqual(domain["record_count"], len(store.transactions_of("u_001")))
        self.assertEqual(
            domain["required_field_count"],
            domain["record_count"] * 4,
        )
        self.assertGreaterEqual(domain["coverage"], 1.0)
        self.assertEqual(domain["weight"], 0.30)

    def test_recommendation_execution_is_blocked_and_returns_cash_safe_plan(self):
        agent = make_agent()
        agent.store.transactions.clear()
        tool = agent.registry.get("wealth.recommend")
        result = tool.run(
            agent.context(),
            amount=50_000,
            horizon="mid",
        )

        self.assertFalse(result.ok)
        self.assertTrue(result.data["blocked_by_data_quality"])
        self.assertEqual(result.data["cash_safe_plan"]["product_id"], "CASH")
        self.assertEqual(result.data["products"], [])


if __name__ == "__main__":
    unittest.main()
