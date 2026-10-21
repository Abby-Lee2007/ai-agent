"""SQLite session and idempotency persistence tests."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from finagent.api.service import AgentSessionService
from finagent.data.sqlite import SQLiteRepository
from tests.support import make_store


class TestSQLitePersistence(unittest.TestCase):
    def test_store_and_idempotency_survive_service_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first_service = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first_service.create_session(
                user_id="u_001",
                channel="h5",
            )["session_id"]
            first_service.send_message(
                session_id,
                "给王强转1块，用微信",
                request_id="start-1",
            )
            first_service.send_message(
                session_id,
                "确认",
                request_id="confirm-1",
            )
            first_service.send_message(
                session_id,
                "明天给王强转1块，用微信",
                request_id="schedule-1",
            )
            first_service.send_message(
                session_id,
                "确认",
                request_id="schedule-confirm-1",
            )

            second_service = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            replay = second_service.send_message(
                session_id,
                "确认",
                request_id="confirm-1",
            )
            balance_reply = second_service.send_message(
                session_id,
                "我还有多少钱",
                request_id="balance-2",
            )
            connection = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                account_rows = connection.execute(
                    "SELECT account_id, source, is_demo FROM accounts WHERE session_id=?",
                    (session_id,),
                ).fetchall()
                holding_count = connection.execute(
                    "SELECT COUNT(*) FROM holdings WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                task_count = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
                audit_count = connection.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertTrue(replay["replayed"])
        self.assertEqual(balance_reply["reply"]["intent"], "account.balance")
        self.assertEqual(
            second_service._sessions[session_id].agent.store.primary_account("u_001").balance,
            32859.52,
        )
        self.assertTrue(
            {
                "accounts",
                "transactions",
                "cards",
                "subscriptions",
                "holdings",
                "tasks",
                "audit_logs",
            }.issubset(tables)
        )
        self.assertGreaterEqual(len(account_rows), 1)
        self.assertTrue(all(row[1] and row[2] for row in account_rows))
        self.assertGreater(holding_count, 0)
        self.assertGreaterEqual(task_count, 1)
        self.assertGreater(audit_count, 0)


if __name__ == "__main__":
    unittest.main()
