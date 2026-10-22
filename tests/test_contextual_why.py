"""Contextual follow-up questions such as '为什么？'."""
from __future__ import annotations

import unittest

from tests.support import make_agent


class TestContextualWhy(unittest.TestCase):
    def test_why_after_anomaly_explains_rule_and_suggestion(self):
        agent = make_agent()
        agent.handle("帮我看看有没有异常交易")

        reply = agent.handle("为什么？")

        self.assertEqual(reply.intent, "explain.why")
        self.assertNotIn("上一条", reply.text)
        self.assertNotIn("您上次的指令", reply.text)
        self.assertIn("判定", reply.text)
        self.assertIn("深夜大额", reply.text)
        self.assertIn("建议", reply.text)

    def test_why_after_bill_analysis_explains_analysis_basis(self):
        agent = make_agent()
        agent.handle("分析我这个月的账单")

        reply = agent.handle("依据是什么")

        self.assertEqual(reply.intent, "explain.why")
        self.assertNotIn("上一条", reply.text)
        self.assertNotIn("您上次的指令", reply.text)
        self.assertIn("账单分析", reply.text)
        self.assertIn("数据范围", reply.text)

    def test_specific_anomaly_why_targets_earlier_anomaly_not_latest_bill(self):
        agent = make_agent()
        agent.handle("帮我看看有没有异常交易")
        agent.handle("分析我这个月的账单")

        reply = agent.handle("为什么账单异常？")

        self.assertEqual(reply.intent, "explain.why")
        self.assertIn("深夜大额", reply.text)
        self.assertIn("异常交易", reply.text)
        self.assertNotIn("上一条", reply.text)
        self.assertNotIn("分析依据是：按期间筛选交易", reply.text)

    def test_specific_anomaly_why_focuses_on_named_merchant(self):
        agent = make_agent()
        agent.handle("帮我看看有没有异常交易")

        reply = agent.handle("为什么深圳华强北数码城这笔交易异常？")

        self.assertIn("深圳华强北数码城", reply.text)
        self.assertIn("深夜大额", reply.text)
        self.assertNotIn("木屋烧烤", reply.text)

    def test_specific_why_without_matching_result_does_not_explain_latest_bill(self):
        agent = make_agent()
        agent.handle("分析我这个月的账单")

        reply = agent.handle("为什么账单异常？")

        self.assertEqual(reply.intent, "explain.why")
        self.assertIn("没有找到", reply.text)
        self.assertIn("异常交易", reply.text)
        self.assertNotIn("分析依据是：按期间筛选交易", reply.text)


if __name__ == "__main__":
    unittest.main()
