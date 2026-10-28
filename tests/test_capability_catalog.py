"""Business capability navigation catalog tests."""
from __future__ import annotations

import unittest

from finagent.nlu.capability_catalog import capability_catalog_payload
from finagent.nlu.intent_parser import IntentParser
from tests.support import make_store


class TestCapabilityCatalog(unittest.TestCase):
    def setUp(self):
        self.payload = capability_catalog_payload()

    def test_six_business_categories_are_in_user_order(self):
        labels = [category["label"] for category in self.payload["categories"]]
        self.assertEqual(labels, [
            "智能转账",
            "账单分析",
            "理财操作",
            "卡片管理",
            "订阅管理",
            "人际礼物",
        ])
        self.assertTrue(all(category["actions"] for category in self.payload["categories"]))

    def test_daily_brief_is_global_not_a_gift_action(self):
        global_ints = {action["intent"] for action in self.payload["global_actions"]}
        self.assertIn("crossscene.daily_brief", global_ints)
        gift_ints = {
            action["intent"]
            for category in self.payload["categories"]
            if category["label"] == "人际礼物"
            for action in category["actions"]
        }
        self.assertNotIn("crossscene.daily_brief", gift_ints)

    def test_catalog_covers_every_user_facing_business_intent(self):
        actual = {
            action["intent"]
            for category in self.payload["categories"]
            for action in category["actions"]
        } | {action["intent"] for action in self.payload["global_actions"]}
        expected = {
            "account.balance",
            "transfer.send",
            "transfer.schedule",
            "transfer.split_aa",
            "transfer.aa_status",
            "transfer.list_scheduled",
            "bill.report",
            "bill.analyze",
            "bill.anomaly",
            "bill.search",
            "bill.scan_receipt",
            "bill.supported_formats",
            "risk.playbook",
            "wealth.risk_assess",
            "wealth.recommend",
            "wealth.compare",
            "wealth.subscribe",
            "wealth.redeem",
            "wealth.portfolio",
            "wealth.analytics",
            "card.list",
            "card.statement",
            "card.timeline",
            "card.apply",
            "card.bind",
            "card.limit_adjust",
            "card.control",
            "card.freeze",
            "card.unlock",
            "card.report_lost",
            "subscription.detect",
            "subscription.list",
            "subscription.cancel",
            "subscription.resume",
            "subscription.change_cycle",
            "subscription.remind",
            "membership.purchase",
            "crossscene.birthday_plan",
            "crossscene.gift_plan",
            "goal.create",
            "goal.list",
            "crossscene.daily_brief",
            "help",
            "profile.show",
            "forecast.cashflow",
            "advisor.proactive",
            "advisor.health_score",
            "advisor.events",
            "account.reconcile",
        }
        self.assertEqual(actual, expected)

    def test_similar_phrases_route_to_the_same_business_intent(self):
        parser = IntentParser(store=make_store(), user_id="u_001", mode="off")
        cases = {
            "给朋友打点钱": "transfer.send",
            "这周的钱都花哪了": "bill.analyze",
            "把信用卡锁上": "card.freeze",
            "不想再自动扣了": "subscription.cancel",
            "给老婆准备一份伴手礼": "crossscene.gift_plan",
            "今天整体有什么要处理": "crossscene.daily_brief",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parser.parse(text).name, expected)

    def test_prompt_actions_use_card_labels_without_example_entities(self):
        prompt_actions = [
            action
            for category in self.payload["categories"]
            for action in category["actions"]
            if action["input_mode"] == "prompt"
        ] + [
            action
            for action in self.payload["global_actions"]
            if action["input_mode"] == "prompt"
        ]
        self.assertTrue(prompt_actions)
        for action in prompt_actions:
            with self.subTest(action=action["id"]):
                self.assertEqual(action["prompt"], action["label"])

    def test_prompt_action_labels_route_to_declared_intent(self):
        parser = IntentParser(store=make_store(), user_id="u_001", mode="off")
        prompt_actions = [
            action
            for category in self.payload["categories"]
            for action in category["actions"]
            if action["input_mode"] == "prompt"
        ] + [
            action
            for action in self.payload["global_actions"]
            if action["input_mode"] == "prompt"
        ]
        for action in prompt_actions:
            with self.subTest(action=action["id"], prompt=action["prompt"]):
                self.assertEqual(
                    parser.parse(action["prompt"]).name,
                    action["intent"],
                )


if __name__ == "__main__":
    unittest.main()
