"""场景三：理财操作（风险评估 / 产品推荐对比 / 一键申购赎回）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent, run_to_completion


class TestWealth(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_risk_assessment(self):
        reply = self.agent.handle("帮我做个风险测评")
        self.assertEqual(reply.intent, "wealth.risk_assess")
        self.assertIn("风险承受能力", reply.text)
        self.assertIn(reply.data["label"],
                      {"C1 保守型", "C2 稳健型", "C3 平衡型", "C4 成长型", "C5 进取型"})

    def test_recommend_products(self):
        reply = run_to_completion(self.agent, self.agent.handle("我有5万块，稳健一点怎么配置"))
        self.assertEqual(reply.intent, "wealth.recommend")
        self.assertTrue(reply.ok)
        self.assertIn("组合配置建议", reply.text)
        self.assertLessEqual(reply.data["max_risk"], 3)
        self.assertEqual(reply.data["invest"], 50000.0)
        self.assertEqual(reply.data["display_count"], len(reply.data["product_details"]))
        self.assertIn("统一产品清单", reply.text)
        self.assertIn("产品说明（用大白话解释）", reply.text)
        self.assertIn("是否继续申购", reply.text)

    def test_recommend_respects_risk_cap(self):
        reply = self.agent.handle("推荐几个理财产品")
        self.assertLessEqual(reply.data["max_risk"], 4)
        self.assertTrue(all(
            item["risk_level"] <= 4
            for item in reply.data["product_details"]
        ))

    def test_recommendation_excludes_unaffordable_products_and_can_switch(self):
        self.agent.risk_level = 1
        reply = self.agent.handle("推荐几个理财产品")
        details = reply.data["product_details"]

        self.assertGreaterEqual(len(details), 3)
        self.assertEqual(reply.data["display_count"], len(details))
        self.assertNotIn("大额存单3年期", [item["name"] for item in details])
        self.assertTrue(all(
            item["min_amount"] <= reply.data["affordable_budget"]
            for item in details
        ))
        self.assertTrue(any(item["requires_risk_confirmation"] for item in details))
        self.assertIn("换一批", reply.data["filter_options"])

        switched = self.agent.handle("推荐债券产品")
        self.assertTrue(switched.ok)
        self.assertEqual(switched.intent, "wealth.recommend")
        self.assertTrue(all(
            item["category"] in {"国债", "可转债", "债券基金"}
            for item in switched.data["product_details"]
        ))

    def test_subscribe_preflight_blocks_below_minimum_before_card_auth(self):
        reply = self.agent.handle("买500块大额存单3年期")
        self.assertFalse(reply.ok)
        self.assertIn("起投金额", reply.text)
        self.assertIsNone(self.agent.session.pending)

    def test_subscribe_requires_confirmation_and_creates_holding(self):
        reply = self.agent.handle("买1万块稳利债券A")
        self.assertEqual(reply.kind, "question")
        self.assertIn("付款银行卡", reply.text)
        reply = self.agent.handle("用工资卡")
        self.assertEqual(reply.kind, "confirm")
        product_id = self.agent.store.find_product("稳利债券A").product_id
        before = sum(h.market_value for h in self.agent.store.holdings_of("u_001", status="holding")
                     if h.product_id == product_id)
        reply = run_to_completion(self.agent, reply)
        self.assertTrue(reply.ok)
        after = sum(h.market_value for h in self.agent.store.holdings_of("u_001", status="holding")
                    if h.product_id == product_id)
        self.assertAlmostEqual(after - before, 10000.0, places=2)
        self.assertIn("申购成功", reply.text)

    def test_subscribe_below_min_amount_rejected(self):
        reply = self.agent.handle("买50块稳利债券A")
        self.assertFalse(reply.ok)
        self.assertIn("起投", reply.text)

    def test_suitability_blocks_high_risk_product(self):
        # R5 产品超出 R3 客户的承受能力两级以上，按适当性管理直接拦截
        reply = self.agent.handle("买5000块科技成长先锋")
        self.assertFalse(reply.ok)
        self.assertIn("风险承受能力", reply.text)

    def test_selected_recommendation_requires_purchase_details_and_updates_assets(self):
        recommendation = self.agent.handle("我有5000块，稳健一点怎么配置")
        self.assertGreaterEqual(len(recommendation.data["product_details"]), 1)

        reply = self.agent.handle("购买第1只")
        self.assertEqual(reply.kind, "question")
        self.assertIn("金额", reply.text)
        reply = self.agent.handle("1000")
        self.assertEqual(reply.kind, "question")
        self.assertIn("付款银行卡", reply.text)
        reply = self.agent.handle("用工资卡")
        self.assertEqual(reply.kind, "confirm")

        card = self.agent.store.find_card("u_001", "工资卡")
        card_before = card.balance
        reply = run_to_completion(self.agent, reply)
        self.assertTrue(reply.ok)
        self.assertAlmostEqual(card.balance, card_before - 1000, places=2)
        self.assertIn("银行卡资产总额", reply.text)
        self.assertAlmostEqual(reply.data["asset_total_after"], 183060.52, places=2)

    def test_portfolio(self):
        reply = self.agent.handle("我的理财持仓")
        self.assertEqual(reply.intent, "wealth.portfolio")
        self.assertIn("持仓总市值", reply.text)
        self.assertGreater(reply.data["total_value"], 0)

    def test_redeem_all(self):
        reply = run_to_completion(self.agent, self.agent.handle("全部赎回"))
        self.assertTrue(reply.ok)
        self.assertEqual(self.agent.store.holdings_of("u_001", status="holding"), [])

    def test_redeem_specific_product(self):
        reply = run_to_completion(self.agent, self.agent.handle("赎回稳利债券A"))
        self.assertTrue(reply.ok)
        self.assertGreater(reply.data["total"], 0)


if __name__ == "__main__":
    unittest.main()
