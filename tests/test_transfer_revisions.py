"""Revise a transfer draft after a risk rejection without restarting."""
from __future__ import annotations

import unittest

from tests.support import make_agent


class TestTransferRevisions(unittest.TestCase):
    def test_blocked_transfer_amount_can_be_revised_and_confirmed(self):
        agent = make_agent()
        before = agent.store.primary_account("u_001").balance

        blocked = agent.handle("给王强转50000块，用微信")

        self.assertEqual(blocked.kind, "error")
        self.assertEqual(agent.session.pending.kind, "blocked_draft")
        self.assertEqual(agent.session.pending.intent, "transfer.send")
        self.assertIn("改成", blocked.text)

        revised = agent.handle("改成3000")

        self.assertEqual(revised.intent, "transfer.send")
        self.assertEqual(revised.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("3,000.00", revised.text)
        self.assertIn("王强", revised.text)
        self.assertIn("微信", revised.text)

        done = agent.handle("确认")
        done = agent.handle("246810")

        self.assertTrue(done.ok)
        self.assertAlmostEqual(
            agent.store.primary_account("u_001").balance,
            before - 3000.0,
            places=2,
        )

    def test_revised_transfer_remains_revisable_if_still_blocked(self):
        agent = make_agent()
        agent.handle("给王强转50000块，用微信")

        revised = agent.handle("改成40000")

        self.assertEqual(revised.kind, "error")
        self.assertEqual(agent.session.pending.kind, "blocked_draft")
        self.assertEqual(agent.session.pending.slots["amount"], 40000.0)
        self.assertIn("40,000.00", revised.text)
        self.assertIn("改成", revised.text)

    def test_bare_number_revises_the_blocked_transfer(self):
        agent = make_agent()
        agent.handle("给王强转50000块，用微信")

        revised = agent.handle("3000")

        self.assertEqual(revised.intent, "transfer.send")
        self.assertEqual(revised.kind, "confirm")
        self.assertIn("3,000.00", revised.text)

    def test_new_business_command_interrupts_blocked_transfer(self):
        agent = make_agent()
        agent.handle("给王强转50000块，用微信")

        reply = agent.handle("我还有多少钱")

        self.assertEqual(reply.intent, "account.balance")
        self.assertIn("总资产", reply.text)
        self.assertIsNone(agent.session.pending)

    def test_unknown_reply_keeps_blocked_draft_for_revision(self):
        agent = make_agent()
        agent.handle("给王强转50000块，用微信")

        reply = agent.handle("那怎么办")

        self.assertEqual(reply.kind, "question")
        self.assertIn("转账草稿", reply.text)
        self.assertIn("改成", reply.text)
        self.assertEqual(agent.session.pending.kind, "blocked_draft")


if __name__ == "__main__":
    unittest.main()
