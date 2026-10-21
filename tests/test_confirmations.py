"""Critical-data confirmation flow tests."""
from __future__ import annotations

import unittest

from tests.support import make_agent


class TestBirthdayConfirmation(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_birthday_requires_explicit_date_even_when_profile_has_it(self):
        reply = self.agent.handle("我老婆快生日了，帮我准备一下")

        self.assertEqual(reply.kind, "question")
        self.assertIsNotNone(self.agent.session.pending)
        self.assertIn("birthday", self.agent.session.pending.missing)
        self.assertEqual(self.agent.store.goals_of("u_001", status="active"), [])

        reply = self.agent.handle("11月20日")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(self.agent.session.pending.kind, "data_confirm")
        self.assertIn("关键信息", reply.text)
        self.assertIn("2026-11-20", reply.text)

        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.kind, "executed")
        self.assertEqual(len(reply.trace), 3)


class TestQuestionnaireConfirmation(unittest.TestCase):
    def test_skip_shows_inferred_answers_before_applying_them(self):
        agent = make_agent(risk_completed=False)
        agent.handle("推荐几个基金")

        reply = agent.handle("跳过")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "questionnaire_confirm")
        self.assertIsNone(agent.risk_level)
        self.assertIn("系统推断", reply.text)
        self.assertIn("年收入", reply.text)
        self.assertIn("依据：", reply.text)
        self.assertIn("置信度：", reply.text)

        reply = agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertIsNotNone(agent.risk_level)
        self.assertIsNone(agent.session.pending)
        self.assertTrue(agent.risk_inference_confirmed_at)
        self.assertTrue(any(
            record["event"] == "risk.inference_confirmed"
            for record in agent.store.audit
        ))


class TestActionDataConfirmation(unittest.TestCase):
    def test_unknown_payment_account_does_not_silently_use_primary(self):
        agent = make_agent()
        before = agent.store.primary_account("u_001").balance

        reply = agent.handle("给王强转1块，用微信，从不存在卡")

        self.assertFalse(reply.ok)
        self.assertIn("付款账户", reply.text)
        self.assertAlmostEqual(agent.store.primary_account("u_001").balance, before, places=2)

    def test_transfer_review_shows_resolved_payee_channel_and_payer(self):
        agent = make_agent()
        account = agent.store.primary_account("u_001")
        before = account.balance

        reply = agent.handle("给王强转1块，用微信")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("王强", reply.text)
        self.assertIn("微信", reply.text)
        self.assertIn("活期账户", reply.text)
        self.assertIn("6222", reply.text)
        self.assertAlmostEqual(account.balance, before, places=2)

        reply = agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertAlmostEqual(account.balance, before - 1.0, places=2)

    def test_card_action_review_names_card_kind_and_tail(self):
        agent = make_agent()

        reply = agent.handle("我的卡丢了，帮我挂失")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("信用卡", reply.text)
        self.assertIn("8877", reply.text)
        self.assertFalse(any(c.status == "lost" for c in agent.store.cards_of("u_001")))

    def test_subscription_review_names_exact_merchant_and_amount(self):
        agent = make_agent()

        reply = agent.handle("取消腾讯视频会员")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("腾讯视频VIP", reply.text)
        self.assertIn("25.00", reply.text)

    def test_redeem_all_review_lists_portfolio_before_execution(self):
        agent = make_agent()
        before = len(agent.store.holdings_of("u_001", status="holding"))

        reply = agent.handle("全部赎回")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("全部持仓", reply.text)
        self.assertIn("微众活期+", reply.text)
        self.assertEqual(len(agent.store.holdings_of("u_001", status="holding")), before)

    def test_card_application_review_exposes_defaults(self):
        agent = make_agent()
        before = len(agent.store.cards_of("u_001"))

        reply = agent.handle("我想申请一张信用卡")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("信用卡", reply.text)
        self.assertIn("20,000", reply.text)
        self.assertIn("银联", reply.text)
        self.assertEqual(len(agent.store.cards_of("u_001")), before)

    def test_card_bind_review_masks_sensitive_values(self):
        agent = make_agent()
        agent.handle("绑定一张工商银行的储蓄卡")
        agent.handle("6222020000000007")

        reply = agent.handle("123456")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("工商银行", reply.text)
        self.assertIn("0007", reply.text)
        self.assertNotIn("6222020000000007", reply.text)
        self.assertNotIn("123456", reply.text)

    def test_subscribe_review_shows_exact_product_and_payer(self):
        agent = make_agent()

        reply = agent.handle("买1万块稳利债券A")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("稳利债券A", reply.text)
        self.assertIn("活期账户", reply.text)
        self.assertIn("6222", reply.text)

    def test_aa_review_shows_channel_total_and_share(self):
        agent = make_agent()
        reply = agent.handle("昨晚聚餐960，我、王强、李思琪AA")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("微信", reply.text)
        self.assertIn("960.00", reply.text)
        self.assertIn("320.00", reply.text)

    def test_goal_review_shows_locked_account(self):
        agent = make_agent()

        reply = agent.handle("锁定2000块做应急金")

        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(agent.session.pending.kind, "data_confirm")
        self.assertIn("2,000.00", reply.text)
        self.assertIn("活期账户", reply.text)
        self.assertIn("6222", reply.text)

    def test_c1_buying_r2_requires_warning_and_cooling_period(self):
        agent = make_agent()
        agent.risk_level = 1
        product = agent.store.find_product("稳利债券A")
        before = sum(
            holding.market_value for holding in agent.store.holdings_of("u_001", status="holding")
            if holding.product_id == product.product_id
        )
        agent.handle("买1万块稳利债券A")

        review = agent.handle("确认")
        self.assertEqual(review.kind, "question")
        self.assertEqual(agent.session.pending.kind, "risk_cooling")
        self.assertIn("冷静期", review.text)

        too_early = agent.handle("确认")
        self.assertIn("尚未结束", too_early.text)
        current = sum(
            holding.market_value for holding in agent.store.holdings_of("u_001", status="holding")
            if holding.product_id == product.product_id
        )
        self.assertAlmostEqual(current, before, places=2)

        agent.clock.advance(hours=24)
        done = agent.handle("确认")
        self.assertTrue(done.ok)
        self.assertEqual(done.kind, "executed")
        after = sum(
            holding.market_value for holding in agent.store.holdings_of("u_001", status="holding")
            if holding.product_id == product.product_id
        )
        self.assertAlmostEqual(after - before, 10000.0, places=2)
        self.assertTrue(any(
            record["event"] == "risk.cooling_started"
            for record in agent.store.audit
        ))
        self.assertTrue(any(
            record["event"] == "risk.cooling_completed"
            for record in agent.store.audit
        ))


if __name__ == "__main__":
    unittest.main()
