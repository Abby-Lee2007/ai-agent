"""Data provenance, product disclosure, and merchant provider tests."""
from __future__ import annotations

import unittest

from tests.support import make_agent, make_store
from finagent.data.market import MarketDataProvider
from finagent.data.providers import (
    MerchantOfferProvider,
    build_data_providers,
)


class TestDataProvenance(unittest.TestCase):
    def setUp(self):
        self.store = make_store()

    def test_seed_records_carry_source_and_demo_labels(self):
        account = self.store.primary_account("u_001")
        transaction = self.store.transactions_of("u_001")[0]
        card = self.store.cards_of("u_001")[0]
        subscription = self.store.subscriptions_of("u_001")[0]

        for record in (account, transaction, card, subscription):
            self.assertTrue(record.source)
            self.assertTrue(record.updated_at)
            self.assertTrue(record.is_demo)
            self.assertFalse(record.is_live)

    def test_demo_product_has_fee_redemption_and_risk_disclosure(self):
        product = self.store.find_product("稳利债券A")

        self.assertGreaterEqual(product.fee_rate, 0.0)
        self.assertTrue(product.redemption_rule)
        self.assertTrue(product.risk_disclosure)
        self.assertTrue(product.source)
        self.assertTrue(product.updated_at)
        self.assertTrue(product.is_demo)


class TestMerchantOfferProvider(unittest.TestCase):
    def test_provider_selects_in_budget_offer_with_inventory(self):
        store = make_store()
        provider = MerchantOfferProvider(store)

        offer = provider.find_offer(
            item="鲜花",
            budget=350.0,
            delivery_date="2026-11-18",
            city="深圳",
        )

        self.assertIsNotNone(offer)
        self.assertLessEqual(offer.price, 350.0)
        self.assertGreater(offer.inventory, 0)
        self.assertIn("深圳", offer.delivery_cities)
        self.assertTrue(offer.source)
        self.assertTrue(offer.updated_at)
        self.assertTrue(offer.is_demo)

    def test_product_output_exposes_fee_redemption_and_data_label(self):
        agent = make_agent()

        reply = agent.handle("推荐几个基金")

        self.assertEqual(reply.intent, "wealth.recommend")
        self.assertIn("费率", reply.text)
        self.assertIn("赎回", reply.text)
        self.assertIn("演示", reply.text)

    def test_registry_covers_bank_market_and_merchant_domains(self):
        store = make_store()
        market = MarketDataProvider(mode="off")

        providers = build_data_providers(store, market)

        self.assertTrue(providers.accounts.of("u_001"))
        self.assertTrue(providers.transactions.of("u_001"))
        self.assertTrue(providers.cards.of("u_001"))
        self.assertTrue(providers.wealth.products())
        self.assertTrue(providers.subscriptions.of("u_001"))
        self.assertTrue(providers.merchants.find_offer(
            "鲜花", 350.0, "2026-11-18", "深圳"
        ))
        self.assertIs(providers.market, market)
        self.assertTrue(providers.bank.is_sandbox)

    def test_balance_output_shows_source_and_demo_label(self):
        agent = make_agent()

        reply = agent.handle("我还有多少钱")

        self.assertEqual(reply.intent, "account.balance")
        self.assertIn("来源", reply.text)
        self.assertIn("演示", reply.text)


if __name__ == "__main__":
    unittest.main()
