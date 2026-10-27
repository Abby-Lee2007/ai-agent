"""Batch membership input parsing tests."""
from __future__ import annotations

import unittest

from finagent.nlu.membership_parser import (
    apply_membership_options,
    apply_membership_renewal_prices,
    parse_membership_items,
    validate_membership_items,
)


class TestMembershipParser(unittest.TestCase):
    def test_parse_multiple_membership_items(self):
        items = parse_membership_items(
            "帮我充腾讯视频3个月60元、Keep12个月199元"
        )
        self.assertEqual(
            [(item.platform, item.term_months, item.purchase_price) for item in items],
            [("腾讯视频", 3, 60.0), ("Keep", 12, 199.0)],
        )

    def test_parse_six_month_custom_platform_with_renewal_price(self):
        items = parse_membership_items(
            "给米游社充6个月98元，自动续费，续费价88元"
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].term_months, 6)
        self.assertTrue(items[0].auto_renew)
        self.assertEqual(items[0].renewal_price, 88.0)

    def test_apply_numbered_auto_renew_options(self):
        items = [
            {"platform": "腾讯视频", "term_months": 3, "purchase_price": 60.0},
            {"platform": "Keep", "term_months": 12, "purchase_price": 199.0},
        ]
        updated = apply_membership_options(
            items,
            "1自动续费，续费价25元；2不自动续费",
        )
        self.assertTrue(updated[0]["auto_renew"])
        self.assertEqual(updated[0]["renewal_price"], 25.0)
        self.assertFalse(updated[1]["auto_renew"])
        self.assertEqual(updated[1]["renewal_price"], 0.0)

    def test_apply_same_price_option(self):
        items = [
            {"platform": "米游社", "term_months": 6, "purchase_price": 98.0},
        ]
        updated = apply_membership_options(
            items,
            "都自动续费，续费价同本次价格",
        )
        self.assertTrue(updated[0]["auto_renew"])
        self.assertEqual(updated[0]["renewal_price"], 98.0)

    def test_apply_numbered_renewal_prices(self):
        items = [
            {
                "platform": "腾讯视频",
                "term_months": 3,
                "purchase_price": 60.0,
                "auto_renew": True,
                "renewal_price": 0.0,
            },
            {
                "platform": "Keep",
                "term_months": 12,
                "purchase_price": 199.0,
                "auto_renew": True,
                "renewal_price": 0.0,
            },
        ]
        updated = apply_membership_renewal_prices(items, "1续费价25元，2续费价199元")
        self.assertEqual(updated[0]["renewal_price"], 25.0)
        self.assertEqual(updated[1]["renewal_price"], 199.0)

    def test_validate_rejects_duplicate_platforms(self):
        errors = validate_membership_items([
            {
                "platform": "米游社",
                "term_months": 3,
                "purchase_price": 30.0,
                "auto_renew": False,
                "renewal_price": 0.0,
            },
            {
                "platform": "米游社",
                "term_months": 12,
                "purchase_price": 100.0,
                "auto_renew": True,
                "renewal_price": 88.0,
            },
        ])
        self.assertTrue(any("重复" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
