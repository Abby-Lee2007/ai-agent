"""Non-blocking behavior for every pending interaction."""
from __future__ import annotations

import unittest

from tests.support import make_agent


class TestPendingInterruptions(unittest.TestCase):
    def test_data_confirmation_can_be_interrupted(self):
        agent = make_agent()
        confirmation = agent.handle("给王强转1块，用微信")
        self.assertEqual(confirmation.kind, "confirm")

        reply = agent.handle("我还有多少钱")

        self.assertEqual(reply.intent, "account.balance")
        self.assertIsNone(agent.session.pending)

    def test_plain_confirmation_can_be_interrupted(self):
        agent = make_agent()
        confirmation = agent.handle("识别这张账单截图 samples/bill_sample.txt")
        self.assertEqual(confirmation.kind, "confirm")

        reply = agent.handle("我还有多少钱")

        self.assertEqual(reply.intent, "account.balance")
        self.assertIsNone(agent.session.pending)

    def test_birthday_slot_can_be_interrupted_by_balance_query(self):
        agent = make_agent()
        prompt = agent.handle("我老婆快生日了，帮我准备一下")
        self.assertEqual(prompt.kind, "question")
        self.assertEqual(agent.session.pending.missing, ["birthday"])

        reply = agent.handle("我还有多少钱")

        self.assertEqual(reply.intent, "account.balance")
        self.assertIn("总资产", reply.text)
        self.assertIsNone(agent.session.pending)

    def test_verify_step_can_be_interrupted_by_card_query(self):
        agent = make_agent()
        agent.handle("给妈妈转25000块，用微信")
        verify = agent.handle("确认")
        self.assertEqual(verify.kind, "verify")

        reply = agent.handle("我有哪些卡")

        self.assertEqual(reply.intent, "card.list")
        self.assertIn("信用卡", reply.text)
        self.assertIsNone(agent.session.pending)

    def test_risk_cooling_can_be_interrupted_by_card_query(self):
        agent = make_agent()
        agent.risk_level = 1
        agent.handle("买1万块稳利债券A")
        cooling = agent.handle("确认")
        self.assertEqual(agent.session.pending.kind, "risk_cooling")

        reply = agent.handle("我有哪些卡")

        self.assertEqual(reply.intent, "card.list")
        self.assertIn("信用卡", reply.text)
        self.assertIsNone(agent.session.pending)

    def test_questionnaire_confirmation_can_be_interrupted(self):
        agent = make_agent(risk_completed=False)
        agent.handle("推荐几个基金")
        confirmation = agent.handle("跳过")
        self.assertEqual(confirmation.kind, "confirm")

        reply = agent.handle("我还有多少钱")

        self.assertEqual(reply.intent, "account.balance")
        self.assertIsNone(agent.session.pending)
        self.assertIsNotNone(agent.paused_questionnaire)
        self.assertEqual(agent.paused_questionnaire.kind, "questionnaire_confirm")


if __name__ == "__main__":
    unittest.main()
