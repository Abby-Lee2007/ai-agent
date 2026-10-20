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

    def test_recommend_respects_risk_cap(self):
        reply = self.agent.handle("推荐几个理财产品")
        self.assertLessEqual(reply.data["max_risk"], 3)

    def test_subscribe_requires_confirmation_and_creates_holding(self):
        reply = self.agent.handle("买1万块稳利债券A")
        self.assertEqual(reply.kind, "confirm")
        product_id = self.agent.store.find_product("稳利债券A").product_id
        before = sum(h.market_value for h in self.agent.store.holdings_of("u_001", status="holding")
                     if h.product_id == product_id)
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        after = sum(h.market_value for h in self.agent.store.holdings_of("u_001", status="holding")
                    if h.product_id == product_id)
        self.assertAlmostEqual(after - before, 10000.0, places=2)
        self.assertIn("申购成功", reply.text)

    def test_subscribe_below_min_amount_rejected(self):
        reply = run_to_completion(self.agent, self.agent.handle("买50块稳利债券A"))
        self.assertFalse(reply.ok)
        self.assertIn("起投", reply.text)

    def test_suitability_blocks_high_risk_product(self):
        # R5 产品超出 R3 客户的承受能力两级以上，按适当性管理直接拦截
        reply = run_to_completion(self.agent, self.agent.handle("买5000块科技成长先锋"))
        self.assertFalse(reply.ok)
        self.assertIn("风险承受能力", reply.text)

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
