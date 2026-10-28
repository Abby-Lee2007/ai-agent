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
        self.assertEqual(reply.data["count"], 4)
        self.assertIn("32,860.52", reply.text)
        self.assertIn("150,000.00", reply.text)
        self.assertIn("银联｜工资卡", reply.text)
        self.assertIn("微众银行｜网购专用虚拟卡", reply.text)

    def test_credit_card_statement_details(self):
        reply = self.agent.handle("查看信用卡账单")

        self.assertEqual(reply.intent, "card.statement")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.data["billing_day"], 5)
        self.assertEqual(reply.data["due_day"], 23)
        self.assertAlmostEqual(reply.data["statement_amount"], 12340.0)
        self.assertIn("最低还款", reply.text)

    def test_virtual_card_instant_issue_and_one_time_pan(self):
        reply = self.agent.handle("办一张工商银行虚拟卡网购用")

        self.assertEqual(reply.intent, "card.apply")
        if reply.kind == "confirm":
            reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIn("虚拟卡号（仅本次展示）", reply.text)
        card = self.agent.store.cards[reply.data["card_id"]]
        self.assertEqual(card.status, "active")
        self.assertEqual(card.virtual_pan_once, "")
        timeline = self.agent.handle(f"查看虚拟卡尾号{card.tail}的卡号")
        self.assertIn("此前已经展示并清除", timeline.text)

    def test_add_existing_card_and_open_new_card_are_different_intents(self):
        bind_agent = make_agent()
        self.assertEqual(bind_agent.handle("添加一张工商银行储蓄卡").intent, "card.bind")

        apply_agent = make_agent()
        reply = apply_agent.handle("开一张工商银行储蓄卡")
        self.assertEqual(reply.intent, "card.apply")
        self.assertEqual(apply_agent.session.pending.slots["kind"], "debit")

    def test_bind_other_bank_card(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        reply = self.agent.handle("6222 0200 0000 0007")
        self.assertEqual(reply.kind, "question")
        self.assertIn("取款密码", reply.text)
        self.agent.handle("123456")
        reply = self.agent.handle("13800138000")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("138****8000", reply.text)
        self.assertNotIn("13800138000", reply.text)
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIn("工商银行", reply.text)
        self.assertIn("0007", reply.text)
        self.assertIn("138****8000", reply.text)
        bound = [c for c in self.agent.store.cards_of("u_001") if c.note.endswith("（绑定）")]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0].tail, "0007")

    def test_bind_rejects_invalid_card_number(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        reply = self.agent.handle("13428697578122432")
        self.assertIn("卡号开头格式不符", reply.text)
        self.assertIn("境内银联储蓄卡通常以 62 开头", reply.text)
        self.assertEqual(reply.kind, "question")
        self.assertIn("card_no", self.agent.session.pending.missing)
        self.assertNotIn("请输入该卡的取款密码", reply.text)
        self.assertNotIn("请提供这张卡在银行预留", reply.text)

    def test_bind_rejects_luhn_failure_before_password(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        reply = self.agent.handle("6222020000000001")
        self.assertIn("Luhn", reply.text)
        self.assertIn("某一位数字输错", reply.text)
        self.assertEqual(reply.kind, "question")
        self.assertIn("card_no", self.agent.session.pending.missing)
        self.assertNotIn("请输入该卡的取款密码", reply.text)

    def test_bind_requires_correct_password(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        self.agent.handle("6222020000000007")
        self.agent.handle("000000")
        self.agent.handle("13800138000")
        reply = self.agent.handle("确认")
        self.assertFalse(reply.ok)
        self.assertIn("密码不正确", reply.text)

    def test_bind_rejects_card_bin_that_mismatches_selected_bank(self):
        self.agent.handle("绑定一张中国银行的银行卡")
        reply = self.agent.handle("6222020000000007")
        self.assertEqual(reply.kind, "question")
        self.assertIn("卡号与发卡行不匹配", reply.text)
        self.assertIn("属于工商银行", reply.text)
        self.assertIn("你选择的是中国银行", reply.text)
        self.assertIn("card_no", self.agent.session.pending.missing)

    def test_bind_china_bank_with_matching_bin_and_asks_phone(self):
        reply = self.agent.handle("绑定一张中国银行的银行卡")
        self.assertEqual(reply.kind, "question")
        self.assertIn("卡号", reply.text)

        reply = self.agent.handle("6216610000000000008")
        self.assertEqual(reply.kind, "question")
        self.assertIn("取款密码", reply.text)

        reply = self.agent.handle("123456")
        self.assertEqual(reply.kind, "question")
        self.assertIn("预留", reply.text)
        self.assertIn("手机号", reply.text)

        reply = self.agent.handle("13800138000")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("中国银行", reply.text)
        self.assertIn("138****8000", reply.text)
        self.assertNotIn("未提供", reply.text)

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIn("绑定成功：中国银行", reply.text)
        bound = [c for c in self.agent.store.cards_of("u_001") if c.note.endswith("（绑定）")]
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0].brand, "中国银行")
        self.assertEqual(bound[0].tail, "0008")

    def test_bind_rejects_phone_that_is_not_11_digits(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        self.agent.handle("6222020000000007")
        self.agent.handle("123456")
        reply = self.agent.handle("1380013800")
        self.assertEqual(reply.kind, "question")
        self.assertIn("当前为 10 位", reply.text)
        self.assertIn("应为 11 位数字", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["phone"])

    def test_bind_rejects_phone_with_invalid_prefix(self):
        self.agent.handle("绑定一张工商银行的储蓄卡")
        self.agent.handle("6222020000000007")
        self.agent.handle("123456")
        reply = self.agent.handle("23800138000")
        self.assertEqual(reply.kind, "question")
        self.assertIn("应以 13-19 开头", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["phone"])

    def test_apply_credit_card(self):
        reply = self.agent.handle("我想申请一张工商银行的信用卡")
        self.assertEqual(reply.kind, "question")
        self.assertIn("信用卡申请", reply.text)
        self.assertIn("税后月收入", reply.text)
        for answer in (
            "13800138000",
            "互联网公司产品经理",
            "25000",
            "3000",
            "30000",
            "日常消费",
            "使用演示流水",
            "同意",
        ):
            reply = self.agent.handle(answer)
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIn("模拟预审完成", reply.text)
        self.assertIn("用途：日常消费", reply.text)
        self.assertIn("演示流水（模拟数据）", reply.text)
        self.assertIn("1-3 个工作日", reply.text)
        self.assertIn("模拟预审结果", reply.text)
        pending = [c for c in self.agent.store.cards_of("u_001") if c.status == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].kind, "credit")

    def test_open_debit_card_uses_separate_flow(self):
        reply = self.agent.handle("开一张工商银行的储蓄卡")
        self.assertEqual(reply.kind, "question")
        self.assertIn("储蓄卡开户", reply.text)
        self.assertIn("账户类型", reply.text)
        self.assertEqual(
            self.agent.session.pending.missing,
            ["phone", "account_class", "branch", "card_password", "card_password_confirm"],
        )

        for answer in ("13800138000", "一类户", "深圳", "739284"):
            reply = self.agent.handle(answer)

        self.assertIn("密码强度：强", reply.text)
        self.assertEqual(self.agent.session.history[-2]["content"], "******")
        reply = self.agent.handle("739284")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("开立储蓄卡", reply.text)
        self.assertIn("密码：已二次确认", reply.text)
        self.assertNotIn("739284", reply.text)
        self.assertNotIn("申请信用卡", reply.text)

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIn("储蓄卡开户成功", reply.text)
        opened = [
            card for card in self.agent.store.cards_of("u_001")
            if card.brand == "工商银行" and card.note.startswith("工商银行I类户")
        ]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].kind, "debit")
        self.assertEqual(opened[0].credit_limit, 0)
        self.assertEqual(opened[0].status, "active")
        self.assertEqual(reply.trace[0]["args"]["card_password"], "******")
        self.assertEqual(reply.trace[0]["args"]["card_password_confirm"], "******")

    def test_open_debit_card_rejects_weak_password_and_mismatch(self):
        reply = self.agent.handle("开一张工商银行的储蓄卡")
        for answer in ("13800138000", "一类户", "深圳"):
            reply = self.agent.handle(answer)

        reply = self.agent.handle("123456")
        self.assertEqual(reply.kind, "question")
        self.assertIn("密码强度：弱", reply.text)
        self.assertIn("重新设置", reply.text)
        self.assertEqual(
            self.agent.session.pending.missing,
            ["card_password", "card_password_confirm"],
        )

        reply = self.agent.handle("739284")
        self.assertIn("密码强度：强", reply.text)
        reply = self.agent.handle("739285")
        self.assertIn("两次输入的密码不一致", reply.text)
        self.assertEqual(
            self.agent.session.pending.missing,
            ["card_password", "card_password_confirm"],
        )

    def test_unspecified_card_type_asks_before_credit_application(self):
        reply = self.agent.handle("我想开一张卡")
        self.assertEqual(reply.kind, "question")
        self.assertIn("请先选择卡种", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["kind"])

        reply = self.agent.handle("储蓄卡")
        self.assertIn("储蓄卡开户", reply.text)
        self.assertIn("发卡行", reply.text)
        self.assertIn("bank", self.agent.session.pending.missing)

    def test_credit_card_requires_consent_before_application(self):
        reply = self.agent.handle("申请一张中国银行信用卡")
        for answer in (
            "13800138000",
            "互联网公司产品经理",
            "25000",
            "3000",
            "30000",
            "旅行",
            "使用演示流水",
            "不同意",
        ):
            reply = self.agent.handle(answer)
        self.assertFalse(reply.ok)
        self.assertIn("未提交", reply.text)
        self.assertIsNone(self.agent.session.pending)
        self.assertFalse(any(
            card.brand == "中国银行" and card.kind == "credit"
            for card in self.agent.store.cards_of("u_001")
        ))

    def test_credit_application_uses_uploaded_transactions_when_selected(self):
        account_id = self.agent.store.accounts_of("u_001")[0].account_id
        for index in range(6):
            self.agent.store.add_transaction(
                user_id="u_001",
                account_id=account_id,
                amount=-(80 + index * 10),
                merchant=f"上传商户{index}",
                category="餐饮",
                timestamp=f"2026-09-{10 + index:02d}T12:00:00",
                tags=["receipt_import"],
                source="upload:user",
                is_demo=False,
            )

        reply = self.agent.handle("申请一张工商银行的信用卡")
        for answer in (
            "13800138000",
            "互联网公司产品经理",
            "25000",
            "3000",
            "30000",
            "日常消费",
            "已上传",
            "同意",
        ):
            reply = self.agent.handle(answer)
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIn("已上传流水", reply.text)
        self.assertIn("6 笔", reply.text)
        self.assertNotIn("演示流水（模拟数据）", reply.text)

    def test_credit_uploaded_statement_requires_confirmed_import(self):
        self.agent.handle("申请一张工商银行的信用卡")
        for answer in (
            "13800138000",
            "互联网公司产品经理",
            "25000",
            "3000",
            "30000",
            "日常消费",
            "已上传",
        ):
            reply = self.agent.handle(answer)
        self.assertEqual(reply.kind, "question")
        self.assertIn("没有已确认导入的消费流水", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["statement_mode"])

    def test_limit_adjust_with_sms(self):
        reply = self.agent.handle("信用卡额度提到3万")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertEqual(reply.kind, "verify")
        reply = self.agent.handle("123456")
        self.assertTrue(reply.ok)
        card = self.agent.store.find_card("u_001", "信用卡")
        self.assertEqual(card.credit_limit, 30000.0)

    def test_limit_adjust_keeps_card_selected_in_follow_up(self):
        reply = self.agent.handle("额度调整")
        self.assertEqual(reply.kind, "question")
        self.assertIn("哪张卡", reply.text)
        self.assertIn("3011", reply.text)

        reply = self.agent.handle("微众银行 · 尾号3011的卡调整到10000")
        self.assertEqual(reply.kind, "question")
        self.assertIn("单笔限额", reply.text)
        self.assertIn("日限额", reply.text)
        self.assertNotIn("8877", reply.text)
        self.assertEqual(self.agent.session.pending.missing, ["limit_type"])

        reply = self.agent.handle("调整日限额")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("虚拟卡", reply.text)
        self.assertIn("3011", reply.text)
        self.assertIn("日限额", reply.text)
        self.assertIn("10,000", reply.text)
        self.assertNotIn("8877", reply.text)

        reply = self.agent.handle("确认")
        if reply.kind == "verify":
            reply = self.agent.handle("123456")
        self.assertTrue(reply.ok)
        self.assertEqual(self.agent.store.find_card("u_001", "3011").daily_limit, 10000.0)
        self.assertEqual(self.agent.store.find_card("u_001", "8877").credit_limit, 50000.0)

    def test_direct_limit_adjust_phrase_does_not_use_default_credit_card(self):
        reply = self.agent.handle("把微众银行尾号3011的卡调整到10000")
        self.assertEqual(reply.intent, "card.limit_adjust")
        self.assertEqual(reply.kind, "question")
        self.assertIn("单笔限额", reply.text)
        self.assertIn("日限额", reply.text)
        self.assertEqual(self.agent.session.pending.slots["card"], "3011")
        self.assertEqual(self.agent.session.pending.slots["new_limit"], 10000.0)

    def test_card_control_shortcut_requires_card_and_explicit_action(self):
        reply = self.agent.handle("线上交易设置")
        self.assertEqual(reply.kind, "question")
        self.assertIn("哪张卡", reply.text)

        reply = self.agent.handle("微众银行尾号3011")
        self.assertEqual(reply.kind, "question")
        self.assertIn("开启还是关闭", reply.text)
        self.assertNotIn("8877", reply.text)

        reply = self.agent.handle("关闭")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("虚拟卡", reply.text)
        self.assertIn("3011", reply.text)
        self.assertIn("线上交易", reply.text)
        self.assertIn("关闭", reply.text)
        self.assertNotIn("8877", reply.text)

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

    def test_freeze_confirmation_cannot_be_silently_replaced_by_transfer(self):
        card = self.agent.store.find_card("u_001", "3011")
        source = self.agent.store.find_card("u_001", "工资卡")
        source_before = source.balance

        self.agent.handle("冻结卡片")
        reply = self.agent.handle("虚拟卡尾号3011，网购专用虚拟卡")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(self.agent.session.pending.intent, "card.freeze")

        reply = self.agent.handle(
            "从工资卡转账300到虚拟卡尾号3011，网购专用虚拟卡"
        )

        self.assertEqual(reply.intent, "card.freeze")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("尚未执行", reply.text)
        self.assertIn("请先回复「确认」", reply.text)
        self.assertEqual(card.status, "active")
        self.assertAlmostEqual(source.balance, source_before, places=2)
        self.assertEqual(self.agent.session.pending.intent, "card.freeze")

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(card.status, "frozen")

        reply = self.agent.handle(
            "从工资卡转账300到虚拟卡尾号3011，网购专用虚拟卡"
        )
        self.assertEqual(reply.intent, "transfer.internal")
        self.assertEqual(reply.kind, "error")
        self.assertIn("尾号3011", reply.text)
        self.assertIn("已冻结", reply.text)
        self.assertAlmostEqual(source.balance, source_before, places=2)

    def test_report_lost_is_two_step_plan(self):
        reply = self.agent.handle("我的微众银行尾号3011虚拟卡丢了，帮我挂失")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(reply.plan, ["card.freeze", "card.report_lost"])
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(len(reply.trace), 2)
        card = self.agent.store.find_card("u_001", "3011")
        self.assertEqual(card.status, "lost")
        # 自动生成了补卡申请
        self.assertTrue([c for c in self.agent.store.cards_of("u_001") if c.status == "pending"])

    def test_lost_card_cannot_be_unfrozen(self):
        run_to_completion(self.agent, self.agent.handle("我的信用卡丢了，帮我挂失"))
        reply = run_to_completion(self.agent, self.agent.handle("解冻信用卡"))
        self.assertFalse(reply.ok)

    def test_credit_card_repayment_uses_debit_card_and_updates_both(self):
        debit = self.agent.store.find_card("u_001", "工资卡")
        credit = self.agent.store.find_card("u_001", "信用卡")
        debit_before = debit.balance
        used_before = credit.used

        reply = self.agent.handle("用工资卡还信用卡3000元")
        self.assertEqual(reply.kind, "confirm")
        reply = self.agent.handle("确认")
        self.assertEqual(self.agent.session.pending.kind, "payment_auth")
        reply = self.agent.handle("246810")

        self.assertTrue(reply.ok)
        self.assertAlmostEqual(debit.balance, debit_before - 3000, places=2)
        self.assertAlmostEqual(credit.used, used_before - 3000, places=2)
        self.assertIn("信用卡还款成功", reply.text)
        self.assertIn("银行卡资产总额", reply.text)

    def test_virtual_card_to_own_credit_card_is_repayment(self):
        source = self.agent.store.find_card("u_001", "3011")
        target = self.agent.store.find_card("u_001", "8877")
        source_before = source.balance
        used_before = target.used

        reply = self.agent.handle(
            "虚拟卡尾号3011，网购专用虚拟卡给信用卡尾号8877，主刷信用卡转500"
        )

        self.assertEqual(reply.intent, "card.repay")
        self.assertEqual(reply.kind, "confirm")
        self.assertIn("信用卡还款", reply.text)
        self.assertIn("3011", reply.text)
        self.assertIn("8877", reply.text)
        self.assertNotIn("数字人民币", reply.text)

        reply = self.agent.handle("确认")
        self.assertEqual(reply.kind, "question")
        reply = self.agent.handle("246810")
        self.assertTrue(reply.ok)
        self.assertAlmostEqual(source.balance, source_before - 500, places=2)
        self.assertAlmostEqual(target.used, used_before - 500, places=2)

    def test_frozen_card_blocks_repayment_and_management(self):
        self.agent.handle("冻结信用卡尾号8877")
        self.agent.handle("确认")

        cases = [
            (
                "虚拟卡尾号3011给信用卡尾号8877转500",
                "card.repay",
                "信用卡还款",
            ),
            (
                "信用卡尾号8877额度调整到30000",
                "card.limit_adjust",
                "额度调整",
            ),
            (
                "关闭信用卡尾号8877的线上交易",
                "card.control",
                "交易开关调整",
            ),
        ]
        for text, expected_intent, expected_action in cases:
            with self.subTest(intent=expected_intent):
                reply = self.agent.handle(text)
                self.assertEqual(reply.intent, expected_intent)
                self.assertEqual(reply.kind, "error")
                self.assertFalse(reply.ok)
                self.assertIn("尾号8877", reply.text)
                self.assertIn("已冻结", reply.text)
                self.assertIn(expected_action, reply.text)
                self.assertIsNone(self.agent.session.pending)


if __name__ == "__main__":
    unittest.main()
