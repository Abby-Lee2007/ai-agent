"""API 会话服务测试。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from finagent.api.service import AgentSessionService
from finagent.data.sqlite import SQLiteRepository
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

    def test_profile_goals_are_listed_and_safe_fields_are_editable(self):
        session_id = self.service.create_session(user_id="u_001", channel="h5")["session_id"]
        before = self.service.get_profile_settings(session_id)
        self.assertGreaterEqual(len(before["goals"]), 1)
        goal = before["goals"][0]

        updated = self.service.update_goal_settings(
            session_id,
            goal["goal_id"],
            {
                "name": "云南毕业旅行基金",
                "target_amount": 9000,
                "deadline": "2026-12-31",
                "status": "active",
                "note": "毕业后出发",
                "locked_amount": 1,
            },
        )

        self.assertEqual(updated["name"], "云南毕业旅行基金")
        self.assertEqual(updated["target_amount"], 9000.0)
        self.assertEqual(updated["deadline"], "2026-12-31")
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["note"], "毕业后出发")
        self.assertEqual(updated["locked_amount"], goal["locked_amount"])

    def test_profile_goal_can_be_created_without_locking_funds(self):
        session_id = self.service.create_session(user_id="u_001", channel="h5")["session_id"]
        account = self.service.store.primary_account("u_001")
        locked_before = account.locked

        created = self.service.create_goal_settings(
            session_id,
            {
                "name": "新电脑基金",
                "target_amount": 12000,
                "deadline": "2027-06-01",
                "status": "active",
                "note": "毕业前攒够",
            },
        )

        self.assertEqual(created["name"], "新电脑基金")
        self.assertEqual(created["target_amount"], 12000.0)
        self.assertEqual(created["locked_amount"], 0.0)
        self.assertEqual(created["account_id"], "")
        self.assertEqual(created["source"], "user:profile")
        self.assertFalse(created["is_demo"])
        self.assertEqual(account.locked, locked_before)

    def test_delete_profile_goal_releases_locked_funds(self):
        session_id = self.service.create_session(user_id="u_001", channel="h5")["session_id"]
        created = self.service.create_goal_settings(
            session_id,
            {"name": "短期目标", "target_amount": 500},
        )
        store = self.service._sessions[session_id].agent.store
        account = store.primary_account("u_001")
        goal = store.goals[created["goal_id"]]
        goal.account_id = account.account_id
        goal.locked_amount = 300.0
        account.locked = 300.0
        store.touch(goal, account)

        deleted = self.service.delete_goal_settings(session_id, goal.goal_id)

        self.assertEqual(deleted["deleted_goal_id"], goal.goal_id)
        self.assertEqual(deleted["released_amount"], 300.0)
        self.assertEqual(account.locked, 0.0)
        self.assertNotIn(goal.goal_id, store.goals)

    def test_failed_idempotency_record_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = SQLiteRepository(Path(temp_dir) / "agent.db")
            service = AgentSessionService(
                make_store(),
                llm_mode="off",
                repository=repository,
            )
            session_id = service.create_session(user_id="u_001", channel="h5")["session_id"]
            repository.save_idempotency(
                session_id,
                "upload-old-file-hash",
                {
                    "session_id": session_id,
                    "request_id": "upload-old-file-hash",
                    "reply": {
                        "ok": False,
                        "kind": "executed",
                        "text": "Excel 已读取，但没有找到可识别的交易列",
                    },
                },
                "2026-10-22T00:00:00",
            )
            result = service.send_message(
                session_id,
                "我还有多少钱",
                request_id="upload-old-file-hash",
            )

        self.assertFalse(result["replayed"])
        self.assertIn("总资产", result["reply"]["text"])


if __name__ == "__main__":
    unittest.main()
