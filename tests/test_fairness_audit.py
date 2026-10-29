"""Six-case controlled fairness and repeatability audit."""
from __future__ import annotations

import unittest

from finagent.analytics.fairness import run_fairness_audit
from tests.support import make_store


class TestFairnessAudit(unittest.TestCase):
    def test_six_cases_repeat_and_five_percent_inputs_do_not_create_large_jumps(self):
        report = run_fairness_audit(make_store())

        self.assertEqual(report["case_count"], 6)
        self.assertTrue(report["repeatable_with_same_environment"])
        rows = {row["case_id"]: row for row in report["cases"]}
        for case_id in (
            "income-minus-5",
            "income-plus-5",
            "balance-minus-5",
            "balance-plus-5",
            "age-plus-10",
        ):
            with self.subTest(case_id=case_id):
                self.assertLessEqual(
                    abs(rows[case_id]["health_delta_vs_baseline"]),
                    10.0,
                )
                self.assertLessEqual(
                    abs(
                        rows[case_id]["risk_asset_weight_delta_vs_baseline"]
                    ),
                    0.50,
                )
        self.assertTrue(report["limitations"])
        controls = report["controlled_experiments"]
        self.assertLessEqual(
            controls["risk_questionnaire_conservative"]["risk_level"],
            controls["risk_questionnaire_aggressive"]["risk_level"],
        )
        self.assertLessEqual(
            controls["cash_buffer_35pct"]["risk_asset_weight"],
            controls["cash_buffer_5pct"]["risk_asset_weight"],
        )


if __name__ == "__main__":
    unittest.main()
