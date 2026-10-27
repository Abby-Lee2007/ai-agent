"""Subscription expiration and automatic renewal tests."""
from __future__ import annotations

import unittest

from tests.support import make_agent


class TestSubscriptionLifecycle(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_pending_cycle_applies_before_auto_renewal(self):
        subscription = next(
            item for item in self.agent.store.subscriptions_of("u_001")
            if item.merchant == "Keep会员"
        )
        subscription.pending_term_months = 6
        subscription.pending_amount = 100.0
        subscription.pending_auto_renew = True
        subscription.pending_effective_at = subscription.expires_at
        self.agent.clock.advance(days=2)

        self.agent.handle("我的订阅都有什么")

        self.assertEqual(subscription.term_months, 6)
        self.assertEqual(subscription.amount, 100.0)
        self.assertEqual(subscription.status, "active")
        self.assertTrue(subscription.auto_renew)
        self.assertEqual(subscription.expires_at, "2027-04-15")
        self.assertEqual(subscription.pending_term_months, 0)
        self.assertEqual(subscription.pending_effective_at, "")

    def test_renewal_failure_does_not_extend_or_debit(self):
        account = self.agent.store.primary_account("u_001")
        subscription = self.agent.store.subscriptions_of("u_001")[0]
        subscription.auto_renew = True
        subscription.amount = account.available + 1
        subscription.expires_at = "2026-10-15"
        subscription.next_charge_date = subscription.expires_at
        self.agent.clock.advance(days=2)
        before_balance = account.balance
        before_expiry = subscription.expires_at

        self.agent.handle("我的订阅都有什么")

        self.assertEqual(subscription.status, "renewal_failed")
        self.assertFalse(subscription.auto_renew)
        self.assertEqual(account.balance, before_balance)
        self.assertEqual(subscription.expires_at, before_expiry)

    def test_auto_renewal_debits_one_term_and_extends_service(self):
        account = self.agent.store.primary_account("u_001")
        subscription = next(
            item for item in self.agent.store.subscriptions_of("u_001")
            if item.merchant == "QQ音乐绿钻"
        )
        subscription.auto_renew = True
        subscription.amount = 15.0
        subscription.term_months = 1
        subscription.expires_at = "2026-10-15"
        subscription.next_charge_date = subscription.expires_at
        for other in self.agent.store.subscriptions_of("u_001"):
            if other.sub_id != subscription.sub_id:
                other.auto_renew = False
        self.agent.clock.advance(days=2)
        before_balance = account.balance

        self.agent.handle("我的订阅都有什么")

        self.assertEqual(subscription.status, "active")
        self.assertTrue(subscription.auto_renew)
        self.assertEqual(subscription.expires_at, "2026-11-15")
        self.assertAlmostEqual(account.balance, before_balance - 15.0, places=2)

    def test_one_time_membership_expires_without_renewal(self):
        account = self.agent.store.primary_account("u_001")
        subscription = self.agent.store.subscriptions_of("u_001")[0]
        subscription.auto_renew = False
        subscription.amount = 19.0
        subscription.expires_at = "2026-10-15"
        subscription.next_charge_date = subscription.expires_at
        self.agent.clock.advance(days=2)
        before_balance = account.balance

        self.agent.handle("我的订阅都有什么")

        self.assertEqual(subscription.status, "expired")
        self.assertFalse(subscription.auto_renew)
        self.assertEqual(account.balance, before_balance)


if __name__ == "__main__":
    unittest.main()
