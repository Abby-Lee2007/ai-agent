"""风控引擎单测：限额、二次确认、身份验证、异常交易识别。"""
from __future__ import annotations

import unittest
from datetime import datetime

from tests.support import make_store
from config import RiskPolicy
from finagent.risk.engine import RiskEngine


class TestTransferRiskRules(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.engine = RiskEngine(self.store, self.store.clock)
        self.user = self.store.get_user("u_001")
        self.account = self.store.primary_account("u_001")
        self.saved_payee = self.store.find_payee("u_001", "老婆")
        self.new_payee = None

    def test_within_limits_passes(self):
        decision = self.engine.check_transfer(self.user, self.account, self.saved_payee, "老婆", 500)
        self.assertFalse(decision.blocked)
        self.assertFalse(decision.requires_confirmation)
        self.assertEqual(decision.level, "low")

    def test_single_limit(self):
        decision = self.engine.check_transfer(
            self.user, self.account, self.saved_payee, "老婆", RiskPolicy.SINGLE_TRANSFER_LIMIT + 1
        )
        self.assertTrue(decision.blocked)
        self.assertIn("单笔转账限额", decision.summary())

    def test_new_payee_limit(self):
        decision = self.engine.check_transfer(
            self.user, self.account, self.new_payee, "陌生商户", 8000
        )
        self.assertTrue(decision.blocked)
        self.assertIn("首次向", decision.summary())

    def test_daily_limit(self):
        self.store.add_transaction(
            user_id="u_001", account_id=self.account.account_id, amount=-190000.0,
            merchant="大额转出", category="转账", timestamp="2026-10-14T09:00:00",
        )
        decision = self.engine.check_transfer(self.user, self.account, self.saved_payee, "老婆", 20000)
        self.assertTrue(decision.blocked)
        self.assertIn("单日累计转账限额", decision.summary())

    def test_saved_payee_threshold_is_higher(self):
        decision = self.engine.check_transfer(self.user, self.account, self.saved_payee, "老婆", 6000)
        self.assertTrue(decision.requires_confirmation)
        self.assertFalse(decision.requires_verification)

    def test_large_transfer_requires_sms(self):
        decision = self.engine.check_transfer(
            self.user, self.account, self.saved_payee, "老婆", RiskPolicy.VERIFY_THRESHOLD + 1
        )
        self.assertTrue(decision.requires_confirmation)
        self.assertTrue(decision.requires_verification)
        self.assertEqual(decision.level, "high")

    def test_night_transfer_is_high_risk(self):
        store = make_store(datetime(2026, 10, 14, 2, 30, 0))
        engine = RiskEngine(store, store.clock)
        decision = engine.check_transfer(
            store.get_user("u_001"), store.primary_account("u_001"),
            store.find_payee("u_001", "老婆"), "老婆", 3000,
        )
        self.assertTrue(decision.requires_confirmation)
        self.assertIn("夜间", decision.summary())

    def test_insufficient_balance(self):
        decision = self.engine.check_transfer(self.user, self.account, self.saved_payee, "老婆", 40000)
        self.assertTrue(decision.blocked)
        self.assertIn("余额不足", decision.summary())


class TestOperationRisk(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.engine = RiskEngine(self.store, self.store.clock)
        self.user = self.store.get_user("u_001")

    def test_high_risk_tools_require_confirmation(self):
        for tool in ("card.report_lost", "wealth.subscribe", "subscription.cancel"):
            with self.subTest(tool=tool):
                decision = self.engine.check_operation(tool, {}, self.user)
                self.assertTrue(decision.requires_confirmation)
                self.assertEqual(decision.level, "high")

    def test_recommendation_amount_does_not_trigger_sms(self):
        # 理财推荐里的金额只是配置建议，不应该要求短信验证
        decision = self.engine.check_operation("wealth.recommend", {"amount": 100000}, self.user)
        self.assertFalse(decision.requires_verification)
        self.assertFalse(decision.requires_confirmation)

    def test_large_subscribe_requires_sms(self):
        decision = self.engine.check_operation(
            "wealth.subscribe", {"amount": 30000}, self.user, self.store.primary_account("u_001")
        )
        self.assertTrue(decision.requires_verification)

    def test_unverified_user_blocked(self):
        self.user.verified = False
        decision = self.engine.check_operation("transfer.send", {"amount": 100}, self.user)
        self.assertTrue(decision.blocked)
        self.assertIn("实名", decision.summary())


class TestAnomalyDetection(unittest.TestCase):
    def setUp(self):
        self.store = make_store()
        self.engine = RiskEngine(self.store, self.store.clock)
        self.rows = self.store.transactions_of("u_001", "2026-09-01", "2026-10-14")

    def test_all_six_kinds_detected(self):
        kinds = {a.kind for a in self.engine.detect_anomalies("u_001", self.rows)}
        for expected in ("night_large", "duplicate_charge", "amount_outlier",
                         "overseas", "subscription_hike", "micro_burst"):
            self.assertIn(expected, kinds)

    def test_anomalies_sorted_by_severity(self):
        anomalies = self.engine.detect_anomalies("u_001", self.rows)
        self.assertEqual(anomalies[0].level, "danger")

    def test_normal_period_has_no_false_positive_noise(self):
        # 只回看最近 2 天时，不应该报出深夜大额/重复扣款
        rows = self.store.transactions_of("u_001", "2026-10-13", "2026-10-14")
        kinds = {a.kind for a in self.engine.detect_anomalies("u_001", rows)}
        self.assertNotIn("night_large", kinds)
        self.assertNotIn("duplicate_charge", kinds)


if __name__ == "__main__":
    unittest.main()
