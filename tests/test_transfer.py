"""场景一：智能转账（按人名/备注转账、定时转账、AA 收款）。"""
from __future__ import annotations

import unittest

from tests.support import make_agent, run_to_completion, send_money


class TestSmartTransfer(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_generic_transfer_shortcut_asks_for_payee(self):
        reply = self.agent.handle("给他人转账")

        self.assertEqual(reply.intent, "transfer.send")
        self.assertEqual(reply.kind, "question")
        self.assertEqual(self.agent.session.pending.missing[0], "payee")
        self.assertNotIn("payee", self.agent.session.pending.slots)
        self.assertIn("谁", reply.text)

        reply = self.agent.handle("他人")
        self.assertEqual(reply.kind, "question")
        self.assertEqual(self.agent.session.pending.missing[0], "payee")
        self.assertNotIn("payee", self.agent.session.pending.slots)

        reply = self.agent.handle("王强")
        self.assertEqual(reply.kind, "question")
        self.assertEqual(self.agent.session.pending.slots.get("payee"), "王强")
        self.assertEqual(self.agent.session.pending.missing[0], "amount")

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

    def test_missing_recipient_account_is_asked_before_confirmation(self):
        reply = self.agent.handle("给新朋友转10块，用微信")
        self.assertEqual(reply.intent, "transfer.send")
        self.assertEqual(reply.kind, "question")
        self.assertEqual(self.agent.session.pending.missing, ["payee_account"])
        self.assertIn("尚未配置微信号", reply.text)
        self.assertIn("确认前不会发起转账", reply.text)

        reply = self.agent.handle("new_friend_wx")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("新朋友", reply.text)
        self.assertIn("new_friend_wx", reply.text)
        self.assertEqual(self.agent.session.pending.kind, "data_confirm")

    def test_balance_is_debited(self):
        account = self.agent.store.primary_account("u_001")
        card = self.agent.store.find_card("u_001", "工资卡")
        before = account.balance
        card_before = card.balance
        reply = send_money(self.agent, "给王强转500块")
        self.assertAlmostEqual(account.balance, before - 500, places=2)
        self.assertAlmostEqual(card.balance, card_before - 500, places=2)
        self.assertAlmostEqual(reply.data["asset_total_after"], 184060.52 - 500, places=2)

    def test_internal_card_transfer_moves_balance_without_changing_total(self):
        source = self.agent.store.find_card("u_001", "工资卡")
        target = self.agent.store.find_card("u_001", "定期存单")
        source_before = source.balance
        target_before = target.balance

        reply = self.agent.handle("从工资卡转1000到定期卡")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertEqual(self.agent.session.pending.kind, "payment_auth")
        reply = self.agent.handle("246810")

        self.assertTrue(reply.ok)
        self.assertAlmostEqual(source.balance, source_before - 1000, places=2)
        self.assertAlmostEqual(target.balance, target_before + 1000, places=2)
        self.assertTrue(reply.data["asset_total_unchanged"])

    def test_internal_transfer_shortcut_with_spoken_particle(self):
        source = next(
            card for card in self.agent.store.cards_of("u_001") if card.tail == "6688"
        )
        target = self.agent.store.find_card("u_001", "工资卡")
        source_before = source.balance
        target_before = target.balance

        reply = self.agent.handle("从定期卡转20到工资卡中")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("6688", reply.text)
        reply = self.agent.handle("确认")
        reply = self.agent.handle("246810")

        self.assertTrue(reply.ok)
        self.assertAlmostEqual(source.balance, source_before - 20, places=2)
        self.assertAlmostEqual(target.balance, target_before + 20, places=2)

    def test_internal_transfer_accepts_xiang_syntax(self):
        source = self.agent.store.find_card("u_001", "工资卡")
        target = self.agent.store.find_card("u_001", "6688")
        source_before = source.balance
        target_before = target.balance

        reply = self.agent.handle("工资卡向借记卡尾号6688转20")
        self.assertEqual(reply.intent, "transfer.internal")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        reply = self.agent.handle("246810")

        self.assertTrue(reply.ok)
        self.assertAlmostEqual(source.balance, source_before - 20, places=2)
        self.assertAlmostEqual(target.balance, target_before + 20, places=2)

    def test_internal_transfer_accepts_gei_syntax_without_payment_channel(self):
        source = self.agent.store.find_card("u_001", "工资卡")
        target = self.agent.store.find_card("u_001", "6688")
        source_before = source.balance
        target_before = target.balance

        reply = self.agent.handle("工资卡给借记卡尾号6688转20")

        self.assertEqual(reply.intent, "transfer.internal")
        self.assertEqual(reply.kind, "confirm")
        self.assertNotIn("微信", reply.text)
        self.assertNotIn("支付宝", reply.text)
        reply = self.agent.handle("确认")
        reply = self.agent.handle("246810")
        self.assertTrue(reply.ok)
        self.assertAlmostEqual(source.balance, source_before - 20, places=2)
        self.assertAlmostEqual(target.balance, target_before + 20, places=2)

    def test_frozen_credit_card_is_explained_before_internal_transfer(self):
        self.agent.handle("冻结信用卡尾号8877")
        self.agent.handle("确认")

        reply = self.agent.handle(
            "信用卡尾号8877，主刷信用卡向借记卡尾号6688，日常消费卡转200"
        )

        self.assertEqual(reply.intent, "transfer.internal")
        self.assertEqual(reply.kind, "error")
        self.assertFalse(reply.ok)
        self.assertIn("已冻结", reply.text)
        self.assertIn("信用卡不能作为普通转账", reply.text)
        self.assertIn("改用借记卡", reply.text)
        self.assertIsNone(self.agent.session.pending)

    def test_frozen_debit_source_is_explained_instead_of_generic_missing_card(self):
        self.agent.handle("冻结借记卡尾号6222")
        self.agent.handle("确认")

        reply = self.agent.handle("从工资卡转10到借记卡尾号6688")

        self.assertEqual(reply.intent, "transfer.internal")
        self.assertEqual(reply.kind, "error")
        self.assertIn("尾号6222", reply.text)
        self.assertIn("已冻结", reply.text)
        self.assertNotIn("未找到指定的付款银行卡", reply.text)
        self.assertIsNone(self.agent.session.pending)

    def test_frozen_credit_card_blocks_external_transfer_before_confirmation(self):
        self.agent.handle("冻结信用卡尾号8877")
        self.agent.handle("确认")

        reply = self.agent.handle("用信用卡尾号8877给王强转10，用微信")

        self.assertEqual(reply.intent, "transfer.send")
        self.assertEqual(reply.kind, "error")
        self.assertIn("尾号8877", reply.text)
        self.assertIn("已冻结", reply.text)
        self.assertIsNone(self.agent.session.pending)

    def test_wrong_payment_password_does_not_move_money(self):
        card = self.agent.store.find_card("u_001", "工资卡")
        before = card.balance
        self.agent.handle("给王强转500块，用微信")
        self.agent.handle("确认")
        reply = self.agent.handle("000000")
        self.assertEqual(reply.kind, "question")
        self.assertIn("交易密码不正确", reply.text)
        self.assertAlmostEqual(card.balance, before, places=2)

    def test_small_transfer_completes_after_data_review(self):
        reply = send_money(self.agent, "给王强转500块")
        self.assertEqual(reply.kind, "executed")
        self.assertIsNone(self.agent.session.pending)

    def test_transaction_has_lifecycle_and_bank_reference(self):
        reply = send_money(self.agent, "给王强转500块")
        txn = self.agent.store.transactions[reply.data["txn_id"]]

        self.assertEqual(txn.lifecycle_status, "completed")
        self.assertTrue(txn.submitted_at)
        self.assertTrue(txn.completed_at)
        self.assertTrue(txn.bank_reference.startswith("BANK-"))

    def test_new_payee_large_transfer_enters_cooling_period(self):
        reply = self.agent.handle("给13900007777转4000，用手机号")
        self.assertEqual(reply.intent, "transfer.send")
        self.assertEqual(self.agent.session.pending.kind, "transfer_cooling")
        self.assertIn("安全冷静期", reply.text)

        self.agent.clock.advance(minutes=10)
        reply = self.agent.handle("确认")
        self.assertEqual(self.agent.session.pending.kind, "payment_auth")

    def test_sms_expires_and_can_be_resent(self):
        agent = self.agent
        agent.handle("给妈妈转25000块，用微信")
        agent.handle("确认")
        agent.handle("246810")
        self.assertEqual(agent.session.pending.kind, "verify")
        agent.clock.advance(minutes=3)
        reply = agent.handle("123456")
        self.assertIn("已过期", reply.text)
        reply = agent.handle("重新发送")
        self.assertIn("重新发送", reply.text)
        reply = agent.handle("123456")
        self.assertTrue(reply.ok)

    def test_scheduled_transfer_creates_task(self):
        reply = send_money(
            self.agent,
            "下个月5号自动给房东转3800房租",
            channel="支付宝",
        )
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
        reply = send_money(self.agent, "给13900007777转9000", channel="手机号")
        self.assertFalse(reply.ok)
        self.assertIn("首次向", reply.text)

    def test_insufficient_balance_blocks(self):
        reply = send_money(self.agent, "给妈妈转40000")
        self.assertFalse(reply.ok)
        self.assertTrue("余额不足" in reply.text or "限额" in reply.text)

    def test_large_transfer_requires_confirm_then_sms(self):
        reply = self.agent.handle("给老婆转25000块，用微信")
        reply = self.agent.handle("确认")
        self.assertEqual(reply.kind, "question")
        reply = self.agent.handle("246810")
        self.assertEqual(reply.kind, "verify")
        reply = self.agent.handle("123456")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.kind, "executed")

    def test_wrong_sms_code_rejected(self):
        self.agent.handle("给老婆转25000块，用微信")
        self.agent.handle("确认")
        self.agent.handle("246810")
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
