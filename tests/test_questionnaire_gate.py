"""理财操作前的风险问卷闸门测试。"""
from __future__ import annotations

import unittest

from tests.support import answer_basic_questionnaire, make_agent


class TestRiskGate(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent(risk_completed=False)

    def test_recommendation_requires_questionnaire(self):
        reply = self.agent.handle("推荐几个基金")
        self.assertEqual(reply.kind, "question")
        self.assertIn("第 1/6 题", reply.text)
        self.assertIn("投资者适当性", reply.text)
        self.assertEqual(self.agent.session.pending.kind, "questionnaire")
        # 问卷没填完前不应该执行任何推荐工具
        self.assertEqual(reply.trace, [])

    def test_risk_assess_also_requires_questionnaire(self):
        reply = self.agent.handle("帮我做个风险测评")
        self.assertEqual(reply.kind, "question")
        self.assertIn("第 1/6 题", reply.text)

    def test_subscribe_requires_questionnaire(self):
        reply = self.agent.handle("买1万块稳利债券A")
        self.assertEqual(reply.kind, "question")
        self.assertIn("第 1/6 题", reply.text)

    def test_full_answers_unlock_recommendation(self):
        self.agent.handle("推荐几个基金")
        reply = answer_basic_questionnaire(self.agent, "b")
        self.assertIn("评估完成", reply.text)
        self.assertIn("C", reply.text)
        self.assertIn("基金", reply.text)
        self.assertIsNone(self.agent.session.pending)
        self.assertIsNotNone(self.agent.risk_level)

    def test_invalid_answer_reasks_same_question(self):
        self.agent.handle("推荐几个基金")
        reply = self.agent.handle("随便说说")
        self.assertEqual(reply.kind, "question")
        self.assertIn("第 1/6 题", reply.text)
        self.assertIn("没看懂", reply.text)

    def test_digit_and_text_answers_are_accepted(self):
        self.agent.handle("推荐几个基金")
        self.assertEqual(self.agent.handle("2").kind, "question")          # 第 2 题
        self.assertEqual(self.agent.handle("B").kind, "question")          # 第 3 题
        reply = self.agent.handle("1-3 年")                                 # 选项原文
        self.assertEqual(reply.kind, "question")
        self.assertIn("第 4/6 题", reply.text)

    def test_skip_uses_inferred_answers(self):
        self.agent.handle("推荐几个基金")
        reply = self.agent.handle("跳过")
        self.assertIn("评估完成", reply.text)
        self.assertIsNotNone(self.agent.risk_level)

    def test_cancel_aborts_the_gate(self):
        self.agent.handle("推荐几个基金")
        reply = self.agent.handle("取消")
        self.assertIn("已取消", reply.text)
        self.assertIsNone(self.agent.session.pending)
        self.assertIsNone(self.agent.risk_level)

    def test_second_request_does_not_ask_again(self):
        self.agent.handle("推荐几个基金")
        answer_basic_questionnaire(self.agent, "b")
        reply = self.agent.handle("推荐几个基金")
        self.assertNotIn("第 1/6 题", reply.text)
        self.assertIsNone(self.agent.session.pending)

    def test_conservative_answers_block_stock_recommendation(self):
        self.agent.handle("推荐几个基金")
        reply = answer_basic_questionnaire(self.agent, "a")   # 全部最低档
        self.assertIn("C1", reply.text)
        reply = self.agent.handle("推荐几只A股股票")
        self.assertTrue(reply.data.get("blocked_by_suitability"))
        self.assertIn("适当性", reply.text)

    def test_answers_are_used_for_the_profile(self):
        self.agent.handle("推荐几个基金")
        answer_basic_questionnaire(self.agent, "d")
        self.assertEqual(self.agent.risk_level, 5)
        self.assertEqual(len(self.agent.risk_answers), 6)


if __name__ == "__main__":
    unittest.main()
