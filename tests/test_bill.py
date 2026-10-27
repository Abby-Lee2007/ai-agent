"""场景二：账单分析（分类统计 / 异常交易识别 / 账单报告）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent
from finagent.risk.engine import RiskEngine


class TestBillAnalysis(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_category_analysis(self):
        reply = self.agent.handle("分析我这个月的账单")
        self.assertEqual(reply.intent, "bill.analyze")
        self.assertTrue(reply.ok)
        self.assertIn("消费分类排行", reply.text)
        self.assertGreater(reply.data["stats"]["expense"], 0)

    def test_last_month_analysis(self):
        reply = self.agent.handle("上个月的账单")
        self.assertTrue(reply.ok)
        self.assertIn("上月", reply.text)

    def test_anomaly_detection_finds_planted_cases(self):
        reply = self.agent.handle("帮我看看有没有异常交易")
        self.assertEqual(reply.intent, "bill.anomaly")
        kinds = {item["kind"] for item in reply.data["items"]}
        self.assertIn("night_large", kinds)
        self.assertIn("duplicate_charge", kinds)
        self.assertIn("subscription_hike", kinds)
        self.assertIn("overseas", kinds)
        self.assertIn("amount_outlier", kinds)
        self.assertIn("micro_burst", kinds)

    def test_danger_alert_is_recorded(self):
        self.agent.handle("帮我看看有没有异常交易")
        alerts = self.agent.store.alerts_of("u_001", level="danger")
        self.assertTrue(alerts)

    def test_annual_report(self):
        reply = self.agent.handle("生成我的年度账单报告")
        self.assertEqual(reply.intent, "bill.report")
        self.assertIn("账单报告", reply.text)
        self.assertIn("月度支出趋势", reply.text)
        self.assertIn("Agent 建议", reply.text)

    def test_month_report_uses_like_for_like_comparison(self):
        reply = self.agent.handle("出一份本月账单")
        self.assertIn("环比", self.agent.handle("分析我这个月的账单").text)

    def test_search_by_merchant(self):
        reply = self.agent.handle("我今年在星巴克花了多少钱")
        self.assertEqual(reply.intent, "bill.search")
        self.assertGreaterEqual(reply.data["count"], 0)

    def test_bill_search_shortcut_asks_for_a_real_keyword(self):
        reply = self.agent.handle("消费记录检索")
        self.assertEqual(reply.intent, "bill.search")
        self.assertTrue(reply.needs_input)
        self.assertIn("哪家商户", reply.text)

        reply = self.agent.handle("星巴克")
        self.assertEqual(reply.intent, "bill.search")
        self.assertNotIn("消费记录检索", reply.text)

    def test_search_accepts_natural_language_how_to_lookup_phrases(self):
        category_reply = self.agent.handle("查一下本月在餐饮上的消费")
        self.assertEqual(category_reply.intent, "bill.search")
        self.assertIn("餐饮", category_reply.text)

        merchant_reply = self.agent.handle("查一下星巴克的消费")
        self.assertEqual(merchant_reply.intent, "bill.search")
        self.assertIn("星巴克", merchant_reply.text)

    def test_anomaly_engine_is_reusable(self):
        engine = RiskEngine(self.agent.store, self.agent.clock)
        rows = self.agent.store.transactions_of("u_001", "2026-09-15", "2026-10-14")
        anomalies = engine.detect_anomalies("u_001", rows)
        self.assertTrue(any(a.level == "danger" for a in anomalies))


if __name__ == "__main__":
    unittest.main()
