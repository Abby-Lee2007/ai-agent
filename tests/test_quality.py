"""Unified metric dictionary and data-quality report tests."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from finagent.analytics.quality import (
    METRIC_DICTIONARY,
    build_data_quality_report,
    write_quality_report,
)
from finagent.data.providers import build_data_providers
from tests.support import make_store


class TestDataQualityReport(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.providers = build_data_providers(self.store)
        self.report = build_data_quality_report(
            self.store,
            self.providers,
            self.store.clock,
            "u_001",
        )

    def test_metric_dictionary_has_required_metadata(self):
        self.assertIn("transactions.sample_coverage", METRIC_DICTIONARY)
        metric = METRIC_DICTIONARY["transactions.sample_coverage"]
        for key in ("definition", "unit", "source", "direction", "threshold"):
            self.assertIn(key, metric)

    def test_quality_report_contains_required_fields(self):
        self.assertIn("overall_score", self.report)
        self.assertIn("status", self.report)
        self.assertIn("blocks_personalization", self.report)
        for domain in self.report["domains"].values():
            self.assertIn("record_count", domain)
            self.assertIn("missing_rate", domain)
            self.assertIn("anomaly_rate", domain)
            self.assertIn("updated_at", domain)
            self.assertIn("sample_coverage", domain)

    def test_quality_report_is_deterministic(self):
        second = build_data_quality_report(
            self.store,
            self.providers,
            self.store.clock,
            "u_001",
        )
        self.assertEqual(self.report, second)

    def test_transaction_coverage_is_measured(self):
        transactions = self.report["domains"]["transactions"]
        self.assertGreater(transactions["record_count"], 0)
        self.assertGreaterEqual(transactions["sample_coverage"], 0.0)
        self.assertLessEqual(transactions["sample_coverage"], 1.0)

    def test_quality_report_can_be_written_as_markdown(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "data_quality.md"
            written = write_quality_report(self.report, path)
            text = written.read_text(encoding="utf-8")
        self.assertIn("Metric Dictionary", text)
        self.assertIn("transactions.sample_coverage", text)
        self.assertIn("missing rate", text.lower())


if __name__ == "__main__":
    unittest.main()
