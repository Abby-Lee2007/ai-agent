"""Membership order domain and persistence tests."""
from __future__ import annotations

import unittest

from tests.support import make_store


class TestMembershipModels(unittest.TestCase):
    def test_membership_order_round_trips_through_snapshot(self):
        store = make_store()
        order = store.add_membership_order(
            user_id="u_001",
            account_id="ACC000001",
            total_amount=159.0,
            status="completed",
        )
        item = store.add_membership_order_item(
            order_id=order.order_id,
            user_id="u_001",
            platform="米游社",
            term_months=6,
            purchase_price=159.0,
            auto_renew=False,
            renewal_price=0.0,
            status="fulfilled",
        )

        restored = make_store()
        restored.load_snapshot(store.snapshot())

        self.assertEqual(restored.membership_orders[order.order_id].total_amount, 159.0)
        self.assertEqual(restored.membership_order_items[item.item_id].term_months, 6)
        self.assertEqual(restored.membership_order_items[item.item_id].platform, "米游社")
        self.assertEqual(
            restored.membership_order_items[item.item_id].source,
            "user:quote",
        )


if __name__ == "__main__":
    unittest.main()
