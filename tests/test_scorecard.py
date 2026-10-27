"""风险测评评分卡测试：打分维度、阈值映射、等级判定与压力测试。"""
from __future__ import annotations

import unittest

from tests.support import make_store
from finagent.risk.scorecard import (
    LEVEL_ALLOCATION,
    LEVEL_LABELS,
    DRAWDOWN_TOLERANCE,
    assess_risk_profile,
)


class TestScorecard(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.today = self.store.clock.today()
        self.user = self.store.get_user("u_001")

    def assess(self, user=None):
        return assess_risk_profile(user or self.user, self.store, self.today)

    def test_default_customer_lands_on_r3(self):
        profile = self.assess()
        self.assertEqual(profile.level, 3)
        self.assertEqual(profile.label, "C3 平衡型")
        # 留出安全边界，避免数据微调就跳档
        self.assertGreaterEqual(profile.total, 60)
        self.assertLessEqual(profile.total, 75)

    def test_dimension_totals_sum_to_total(self):
        profile = self.assess()
        summed = sum(score for score, _ in profile.dimension_totals.values())
        self.assertAlmostEqual(summed, profile.total, places=1)
        self.assertAlmostEqual(sum(max_score for _, max_score in profile.dimension_totals.values()),
                               100.0, places=1)

    def test_every_item_is_explainable(self):
        profile = self.assess()
        self.assertEqual(len(profile.items), 14)
        for item in profile.items:
            self.assertGreater(item.max_score, 0)
            self.assertGreaterEqual(item.score, 0)
            self.assertLessEqual(item.score, item.max_score)
            self.assertTrue(item.detail.strip())

    def test_five_dimensions_present(self):
        profile = self.assess()
        self.assertEqual(
            list(profile.dimension_totals.keys()),
            ["财务状况", "投资经验", "风险态度", "投资期限", "流动性需求"],
        )

    def test_risk_attitude_is_monotonic(self):
        user = self.store.get_user("u_001")
        totals = []
        for attitude in ["保守型", "稳健型", "平衡型", "成长型", "进取型"]:
            user.risk_profile = attitude
            totals.append(self.assess(user).total)
        self.assertEqual(totals, sorted(totals))
        self.assertGreater(totals[-1] - totals[0], 10)

    def test_aggressive_young_high_income_scores_higher(self):
        baseline = self.assess().total
        user = self.store.get_user("u_001")
        user.risk_profile = "进取型"
        user.age = 28
        user.monthly_income = 40000.0
        profile = self.assess(user)
        self.assertGreater(profile.total, baseline)
        self.assertGreaterEqual(profile.level, 3)

    def test_elderly_low_income_scores_lower(self):
        user = self.store.get_user("u_002")   # 稳健型
        user.age = 68
        user.monthly_income = 4000.0
        user.risk_profile = "保守型"
        profile = self.assess(user)
        self.assertLessEqual(profile.level, 2)

    def test_level_drives_allocation_and_tolerance(self):
        profile = self.assess()
        self.assertEqual(profile.allocation, LEVEL_ALLOCATION[profile.level])
        self.assertEqual(profile.drawdown_tolerance, DRAWDOWN_TOLERANCE[profile.level])
        self.assertEqual(profile.label, LEVEL_LABELS[profile.level])

    def test_stress_test_uses_total_assets(self):
        profile = self.assess()
        self.assertAlmostEqual(
            profile.stress_loss,
            round(profile.total_assets * profile.drawdown_tolerance, 2),
            places=2,
        )
        self.assertTrue(any("回撤压力测试" in note for note in profile.notes))

    def test_level_never_out_of_range(self):
        for user_id in ("u_001", "u_002", "u_003", "u_004"):
            with self.subTest(user=user_id):
                profile = self.assess(self.store.get_user(user_id))
                self.assertIn(profile.level, {1, 2, 3, 4, 5})


class TestRiskAssessTool(unittest.TestCase):
    def test_tool_reports_scorecard_breakdown(self):
        from tests.support import make_agent
        agent = make_agent()
        reply = agent.handle("帮我做个风险测评")
        self.assertTrue(reply.ok)
        self.assertIn("打分明细", reply.text)
        self.assertIn("逐项依据", reply.text)
        self.assertIn("回撤压力测试", reply.text)
        self.assertEqual(reply.data["level"], 3)
        self.assertEqual(len(reply.data["dimensions"]), 5)

    def test_recommendation_respects_scorecard_level(self):
        from tests.support import make_agent
        agent = make_agent()
        reply = agent.handle("推荐几个理财产品")
        self.assertLessEqual(reply.data["max_risk"], 4)
        self.assertTrue(all(
            item["risk_level"] <= 4 for item in reply.data["product_details"]
        ))


if __name__ == "__main__":
    unittest.main()
