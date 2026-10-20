"""场景一：智能转账（按人名/备注转账、定时转账、AA 收款）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent, run_to_completion, send_money


class TestSmartTransfer(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_transfer_by_alias_and_note(self):
        reply = send_money(self.agent, "给老婆转2000块，备注生日礼物")
        self.assertEqual(reply.intent, "transfer.send")
        self.assertTrue(reply.text.startswith("转账成功"))
        txn = self.agent.store.transactions_of("u_001")[0]
        self.assertEqual(txn.amount, -2000.0)
        self.assertEqual(txn.merchant, "林雅雯")

    def test_transfer_by_phone(self):
        reply = send_money(self.agent, "给13700003333转800")
        self.assertTrue(reply.ok)
        self.assertIn("王强", reply.text)

    def test_balance_is_debited(self):
        account = self.agent.store.primary_account("u_001")
        before = account.balance
        send_money(self.agent, "给王强转500块")
        self.assertAlmostEqual(account.balance, before - 500, places=2)

    def test_small_transfer_completes_after_data_review(self):
        reply = send_money(self.agent, "给王强转500块")
        self.assertEqual(reply.kind, "executed")
        self.assertIsNone(self.agent.session.pending)

    def test_scheduled_transfer_creates_task(self):
        reply = send_money(self.agent, "下个月5号自动给房东转3800房租")
        self.assertEqual(reply.intent, "transfer.schedule")
        self.assertEqual(reply.kind, "executed")
        self.assertTrue(reply.ok)
        tasks = self.agent.store.tasks_of("u_001", status="pending")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].run_date, "2026-11-05")
        self.assertEqual(tasks[0].payload["amount"], 3800.0)

    def test_split_aa(self):
        reply = self.agent.handle("昨晚聚餐960，我、王强、李思琪AA")
        self.assertEqual(reply.intent, "transfer.split_aa")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        sheet = list(self.agent.store.aa_sheets.values())[0]
        self.assertEqual(sheet.total_amount, 960.0)
        self.assertEqual(len(sheet.participants), 3)
        self.assertAlmostEqual(reply.data["share"] if reply.data else 320.0, 320.0)

    def test_missing_amount_triggers_followup(self):
        reply = self.agent.handle("给王强转点钱")
        self.assertEqual(reply.kind, "question")
        self.assertIn("amount", self.agent.session.pending.missing)
        reply = self.agent.handle("600")
        reply = self.agent.handle("微信")
        reply = run_to_completion(self.agent, reply)
        self.assertTrue(reply.ok)
        self.assertEqual(self.agent.store.transactions_of("u_001")[0].amount, -600.0)


class TestTransferRisk(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_single_limit_blocks(self):
        reply = send_money(self.agent, "给王强转60000")
        self.assertFalse(reply.ok)
        self.assertIn("单笔转账限额", reply.text)
        self.assertEqual(len(self.agent.store.transactions_of("u_001")), self._baseline())

    def _baseline(self) -> int:
        return len(self.agent.store.transactions_of("u_001"))

    def test_new_payee_limit_blocks(self):
        reply = send_money(self.agent, "给赵老板转9000")
        self.assertFalse(reply.ok)
        self.assertIn("首次向", reply.text)

    def test_insufficient_balance_blocks(self):
        reply = send_money(self.agent, "给妈妈转40000")
        self.assertFalse(reply.ok)
        self.assertTrue("余额不足" in reply.text or "限额" in reply.text)

    def test_large_transfer_requires_confirm_then_sms(self):
        reply = send_money(self.agent, "给老婆转25000块")
        self.assertEqual(reply.kind, "verify")
        reply = self.agent.handle("123456")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.kind, "executed")

    def test_wrong_sms_code_rejected(self):
        send_money(self.agent, "给老婆转25000块")
        reply = self.agent.handle("000000")
        self.assertEqual(reply.kind, "verify")
        self.assertIn("不正确", reply.text)

    def test_deny_cancels_operation(self):
        agent = self.agent
        reply = agent.handle("下个月5号自动给房东转3800房租，用银行卡")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        count_before = len(self.agent.store.transactions_of("u_001"))
        reply = self.agent.handle("取消")
        self.assertIn("取消", reply.text)
        self.assertEqual(len(self.agent.store.tasks_of("u_001", status="pending")), 0)
        self.assertEqual(len(self.agent.store.transactions_of("u_001")), count_before)


if __name__ == "__main__":
    unittest.main()
