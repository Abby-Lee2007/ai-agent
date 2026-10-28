"""Validated LLM tool-calling planner tests."""
from __future__ import annotations

import unittest

from finagent.agent.llm_planner import LLMToolPlanner
from finagent.agent.registry import build_default_registry


class FakeToolLLM:
    def __init__(self, name: str, arguments=None):
        self.name = name
        self.arguments = arguments or {}
        self.last_error = ""

    @property
    def available(self) -> bool:
        return True

    def chat_with_tools(self, messages, tools, tool_choice="auto"):
        return {"name": self.name, "arguments": self.arguments}


class TestLLMToolPlanner(unittest.TestCase):
    def setUp(self):
        self.registry = build_default_registry()

    def test_accepts_authorized_tool(self):
        llm = FakeToolLLM("account__balance", {})
        planner = LLMToolPlanner(self.registry, llm)

        proposal = planner.propose(
            "account.balance",
            [("account.balance", {})],
            {},
            "我有哪些钱",
        )

        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["tool"], "account.balance")
        self.assertEqual(proposal["args"], {})

    def test_rejects_tool_outside_candidate_allowlist(self):
        llm = FakeToolLLM("transfer__send", {"amount": 1})
        planner = LLMToolPlanner(self.registry, llm)

        proposal = planner.propose(
            "account.balance",
            [("account.balance", {})],
            {},
            "我有哪些钱",
        )

        self.assertIsNone(proposal)
        self.assertIn("未授权", planner.last_error)

    def test_deterministic_slots_override_model_arguments(self):
        llm = FakeToolLLM(
            "card__freeze",
            {"card": "9999", "reason": "模型猜测"},
        )
        planner = LLMToolPlanner(self.registry, llm)

        proposal = planner.propose(
            "card.freeze",
            [("card.freeze", {"card": "card", "reason": "reason"})],
            {"card": "8877"},
            "冻结尾号8877",
        )

        self.assertEqual(proposal["args"]["card"], "8877")
        self.assertEqual(proposal["args"]["reason"], "模型猜测")


if __name__ == "__main__":
    unittest.main()
