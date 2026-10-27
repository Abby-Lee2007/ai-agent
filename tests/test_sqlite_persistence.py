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
                "246810",
                request_id="password-1",
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
            first_service.send_message(
                session_id,
                "246810",
                request_id="schedule-password-1",
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
        self.assertTrue(all(row[1] for row in account_rows))
        self.assertTrue(all(
            (not row[2]) if row[0] == "ACC000005" else bool(row[2])
            for row in account_rows
        ))
        self.assertGreater(holding_count, 0)
        self.assertGreaterEqual(task_count, 1)
        self.assertGreater(audit_count, 0)

    def test_paused_questionnaire_survives_service_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session(user_id="u_001", channel="h5")["session_id"]
            first.send_message(session_id, "推荐几个基金", request_id="risk-start")
            first.send_message(session_id, "b", request_id="risk-answer-1")
            first.send_message(session_id, "我还有多少钱", request_id="risk-other-command")

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            resumed_prompt = second.send_message(
                session_id,
                "推荐几个基金",
                request_id="risk-resume-prompt",
            )
            self.assertEqual(resumed_prompt["reply"]["kind"], "question")
            self.assertIn("上次进度：已答 1/7", resumed_prompt["reply"]["text"])
            self.assertEqual(
                second._sessions[session_id].agent.session.pending.kind,
                "questionnaire_resume",
            )

    def test_why_followup_survives_service_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session(user_id="u_001", channel="h5")["session_id"]
            first.send_message(session_id, "我有哪些卡", request_id="cards-context")

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            why = second.send_message(session_id, "为什么？", request_id="cards-why")

        self.assertEqual(why["reply"]["intent"], "explain.why")
        self.assertNotIn("上一条", why["reply"]["text"])
        self.assertIn("卡片", why["reply"]["text"])

    def test_specific_why_history_survives_service_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "agent.db"
            first = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            session_id = first.create_session(user_id="u_001", channel="h5")["session_id"]
            first.send_message(
                session_id,
                "帮我看看有没有异常交易",
                request_id="anomaly-context",
            )
            first.send_message(
                session_id,
                "分析我这个月的账单",
                request_id="bill-context",
            )

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            why = second.send_message(
                session_id,
                "为什么账单异常？",
                request_id="anomaly-why-after-restart",
            )

        self.assertEqual(why["reply"]["intent"], "explain.why")
        self.assertIn("深夜大额", why["reply"]["text"])
        self.assertNotIn("分析依据是：按期间筛选交易", why["reply"]["text"])


if __name__ == "__main__":
    unittest.main()
