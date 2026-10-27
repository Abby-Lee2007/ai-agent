"""Combined membership purchase execution tests."""
from __future__ import annotations

import unittest

from tests.support import make_agent


class TestMembershipPurchase(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_batch_purchase_creates_one_debit_and_two_subscriptions(self):
        before_transactions = len(self.agent.store.transactions_of("u_001"))
        before_balance = self.agent.store.primary_account("u_001").balance
        tool = self.agent.registry.get("membership.purchase")
        self.assertIsNotNone(tool)

        result = tool.run(
            self.agent.context(),
            items=[
                {
                    "platform": "米游社",
                    "term_months": 6,
                    "purchase_price": 98,
                    "auto_renew": True,
                    "renewal_price": 88,
                },
                {
                    "platform": "Lofter",
                    "term_months": 3,
                    "purchase_price": 30,
                    "auto_renew": False,
                    "renewal_price": 0,
                },
            ],
        )

        self.assertTrue(result.ok, result.message)
        transactions = self.agent.store.transactions_of("u_001")
        self.assertEqual(len(transactions), before_transactions + 1)
        self.assertAlmostEqual(
            self.agent.store.primary_account("u_001").balance,
            before_balance - 128,
            places=2,
        )
        orders = self.agent.store.membership_orders_of("u_001")
        self.assertEqual(len(orders), 1)
        self.assertEqual(len(self.agent.store.membership_order_items_of(
            orders[0].order_id
        )), 2)

        miyoushe = next(
            sub for sub in self.agent.store.subscriptions_of("u_001")
            if sub.merchant == "米游社"
        )
        lofter = next(
            sub for sub in self.agent.store.subscriptions_of("u_001")
            if sub.merchant == "Lofter"
        )
        self.assertTrue(miyoushe.auto_renew)
        self.assertEqual(miyoushe.term_months, 6)
        self.assertEqual(miyoushe.amount, 88.0)
        self.assertEqual(miyoushe.expires_at, "2027-04-14")
        self.assertFalse(lofter.auto_renew)
        self.assertEqual(lofter.term_months, 3)
        self.assertEqual(lofter.expires_at, "2027-01-14")

    def test_purchase_rejects_duplicate_platforms_before_debit(self):
        before = len(self.agent.store.transactions_of("u_001"))
        tool = self.agent.registry.get("membership.purchase")
        result = tool.run(
            self.agent.context(),
            items=[
                {
                    "platform": "米游社",
                    "term_months": 3,
                    "purchase_price": 30,
                    "auto_renew": False,
                    "renewal_price": 0,
                },
                {
                    "platform": "米游社",
                    "term_months": 12,
                    "purchase_price": 100,
                    "auto_renew": True,
                    "renewal_price": 88,
                },
            ],
        )
        self.assertFalse(result.ok)
        self.assertIn("重复", result.message)
        self.assertEqual(len(self.agent.store.transactions_of("u_001")), before)

    def test_purchase_extends_existing_subscription_from_current_expiry(self):
        tool = self.agent.registry.get("membership.purchase")
        result = tool.run(
            self.agent.context(),
            items=[
                {
                    "platform": "腾讯视频VIP",
                    "term_months": 3,
                    "purchase_price": 70,
                    "auto_renew": False,
                    "renewal_price": 0,
                },
            ],
        )

        self.assertTrue(result.ok, result.message)
        subscription = next(
            item for item in self.agent.store.subscriptions_of("u_001")
            if item.merchant == "腾讯视频VIP"
        )
        self.assertEqual(subscription.expires_at, "2027-02-08")
        self.assertEqual(subscription.term_months, 3)
        self.assertFalse(subscription.auto_renew)


if __name__ == "__main__":
    unittest.main()
