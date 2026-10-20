"""API 会话服务测试。"""
from __future__ import annotations

import unittest

from finagent.api.service import AgentSessionService
from tests.support import make_store


class TestAgentSessionService(unittest.TestCase):
    def setUp(self):
        self.service = AgentSessionService(make_store(), llm_mode="off")

    def test_create_session_returns_isolated_session_id(self):
        first = self.service.create_session(user_id="u_001", channel="h5")
        second = self.service.create_session(user_id="u_001", channel="h5")

        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["user_id"], "u_001")
        self.assertEqual(first["channel"], "h5")

    def test_duplicate_request_id_does_not_transfer_twice(self):
        session_id = self.service.create_session(user_id="u_001", channel="h5")["session_id"]
        account = self.service.store.primary_account("u_001")
        before = account.balance

        first = self.service.send_message(
            session_id=session_id,
            message="给王强转1块，用微信",
            request_id="retry-safe-001",
        )
        self.assertEqual(first["reply"]["kind"], "confirm")
        self.assertAlmostEqual(account.balance, before, places=2)

        first_replay = self.service.send_message(
            session_id=session_id,
            message="给王强转1块，用微信",
            request_id="retry-safe-001",
        )
        self.assertTrue(first_replay["replayed"])
        self.assertAlmostEqual(account.balance, before, places=2)

        completed = self.service.send_message(
            session_id=session_id,
            message="确认",
            request_id="retry-safe-confirm",
        )
        after_first = account.balance
        second = self.service.send_message(
            session_id=session_id,
            message="确认",
            request_id="retry-safe-confirm",
        )

        self.assertEqual(completed["reply"]["text"], second["reply"]["text"])
        self.assertAlmostEqual(after_first, before - 1.0, places=2)
        self.assertAlmostEqual(account.balance, after_first, places=2)

    def test_im_user_keeps_one_stable_session(self):
        first = self.service.handle_channel_message(
            channel="wecom",
            external_user_id="wx-user-001",
            message="我有哪些卡",
            message_id="msg-001",
        )
        second = self.service.handle_channel_message(
            channel="wecom",
            external_user_id="wx-user-001",
            message="我有哪些卡",
            message_id="msg-002",
        )

        self.assertEqual(first["session_id"], second["session_id"])
        self.assertIn("信用卡", first["reply"]["text"])

    def test_session_store_factory_isolates_browser_data(self):
        service = AgentSessionService(
            make_store(),
            llm_mode="off",
            store_factory=make_store,
        )
        first = service.create_session(user_id="u_001", channel="h5")["session_id"]
        second = service.create_session(user_id="u_001", channel="h5")["session_id"]

        first_store = service._sessions[first].agent.store
        second_store = service._sessions[second].agent.store
        first_store.primary_account("u_001").balance -= 100

        self.assertIsNot(first_store, second_store)
        self.assertNotEqual(
            first_store.primary_account("u_001").balance,
            second_store.primary_account("u_001").balance,
        )


if __name__ == "__main__":
    unittest.main()
