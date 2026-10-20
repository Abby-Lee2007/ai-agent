"""场景四：卡片管理（卡列表 / 额度调整 / 交易限制 / 挂失）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent, run_to_completion


class TestCardManagement(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_card_list(self):
        reply = self.agent.handle("我有哪些卡")
        self.assertEqual(reply.intent, "card.list")
        self.assertEqual(reply.data["count"], 3)

    def test_bind_other_bank_card(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        self.agent.handle("6222020000000007")
        self.agent.handle("123456")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIn("工商银行", reply.text)
        self.assertIn("0007", reply.text)
        bound = [c for c in self.agent.store.cards_of("u_001") if c.note.endswith("（绑定）")]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0].tail, "0007")

    def test_bind_rejects_invalid_card_number(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        self.agent.handle("6222020000000001")
        self.agent.handle("123456")
        reply = self.agent.handle("确认")
        self.assertFalse(reply.ok)
        self.assertIn("校验未通过", reply.text)

    def test_bind_requires_correct_password(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        self.agent.handle("6222020000000007")
        self.agent.handle("000000")
        reply = self.agent.handle("确认")
        self.assertFalse(reply.ok)
        self.assertIn("密码不正确", reply.text)

    def test_apply_credit_card(self):
        reply = self.agent.handle("我想申请一张信用卡")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIn("申请已提交", reply.text)
        pending = [c for c in self.agent.store.cards_of("u_001") if c.status == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, "credit")

    def test_limit_adjust_with_sms(self):
        reply = self.agent.handle("信用卡额度提到3万")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertEqual(reply.kind, "verify")
        reply = self.agent.handle("123456")
        self.assertTrue(reply.ok)
        card = self.agent.store.find_card("u_001", "信用卡")
        self.assertEqual(card.credit_limit, 30000.0)

    def test_turn_off_overseas_transactions(self):
        reply = self.agent.handle("关闭信用卡的境外交易")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertFalse(self.agent.store.find_card("u_001", "信用卡").overseas_enabled)

    def test_turn_on_online_transactions_for_virtual_card(self):
        card = self.agent.store.find_card("u_001", "虚拟卡")
        card.online_enabled = False
        self.agent.handle("把虚拟卡线上交易打开")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertTrue(self.agent.store.find_card("u_001", "虚拟卡").online_enabled)

    def test_freeze_and_unfreeze(self):
        self.agent.handle("把信用卡冻结")
        self.agent.handle("确认")
        self.assertEqual(self.agent.store.find_card("u_001", "信用卡").status, "frozen")
        self.agent.handle("解冻信用卡")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(self.agent.store.find_card("u_001", "信用卡").status, "active")

    def test_report_lost_is_two_step_plan(self):
        reply = self.agent.handle("我的卡丢了，帮我挂失")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(reply.plan, ["card.freeze", "card.report_lost"])
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(len(reply.trace), 2)
        card = self.agent.store.find_card("u_001", "信用卡")
        self.assertEqual(card.status, "lost")
        # 自动生成了补卡申请
        self.assertTrue([c for c in self.agent.store.cards_of("u_001") if c.status == "pending"])

    def test_lost_card_cannot_be_unfrozen(self):
        run_to_completion(self.agent, self.agent.handle("我的卡丢了，帮我挂失"))
        reply = run_to_completion(self.agent, self.agent.handle("解冻信用卡"))
        self.assertFalse(reply.ok)


if __name__ == "__main__":
    unittest.main()
