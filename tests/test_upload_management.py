"""Uploaded bill batch management tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from finagent.agent.core import BankingAgent
from finagent.api.service import AgentSessionService
from finagent.data.sqlite import SQLiteRepository
from tests.support import make_agent, make_store

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_TEXT = PROJECT_ROOT / "samples" / "bill_sample.txt"


class TestUploadBatchModel(unittest.TestCase):
    def test_legacy_imported_transactions_backfill_to_upload_balance_once(self):
        store = make_store()
        ledger = next(
            account for account in store.accounts_of("u_001")
            if account.name == "上传账单余额"
        )
        first = store.add_transaction(
            user_id="u_001",
            account_id="ACC000001",
            amount=-38.0,
            merchant="星巴克咖啡",
            category="餐饮",
            tags=["receipt_import", "legacy"],
            source="upload:user",
            is_demo=False,
        )
        second = store.add_transaction(
            user_id="u_001",
            account_id="ACC000001",
            amount=100.0,
            merchant="退款",
            category="收入",
            tags=["receipt_import", "legacy"],
            source="upload:user",
            is_demo=False,
        )
        batch = store.add_upload_batch(
            user_id="u_001",
            filename="legacy.csv",
            record_count=2,
            total_expense=38.0,
            backend="csv_file",
            transaction_ids=[first.txn_id, second.txn_id],
        )

        BankingAgent(store, "u_001", llm_mode="off")
        BankingAgent(store, "u_001", llm_mode="off")

        self.assertAlmostEqual(ledger.balance, 62.0, places=2)
        self.assertEqual(first.account_id, ledger.account_id)
        self.assertEqual(second.account_id, ledger.account_id)
        self.assertEqual(
            len([
                record for record in store.audit
                if record.get("event") == "upload_ledger.backfilled"
            ]),
            1,
        )
        self.assertEqual(batch.batch_id, "UPL000001")

    def test_import_applies_net_amount_to_upload_balance_and_delete_reverses(self):
        agent = make_agent()
        balance_account = next(
            account for account in agent.store.accounts_of("u_001")
            if account.name == "上传账单余额"
        )
        self.assertEqual(balance_account.balance, 0.0)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mixed.csv"
            path.write_text(
                "日期,商户,金额,收/支\n"
                "2026-10-12,星巴克咖啡,38.00,支出\n"
                "2026-10-13,退款,100.00,收入\n",
                encoding="utf-8",
            )
            scan = agent.handle(f"识别这张账单截图 {path}")
            self.assertEqual(scan.kind, "confirm")
            imported = agent.handle("确认")

        self.assertEqual(imported.data["imported"], 2)
        self.assertAlmostEqual(balance_account.balance, 62.0, places=2)
        batch = agent.store.upload_batches_of("u_001")[0]
        transactions = [
            txn for txn in agent.store.transactions_of("u_001")
            if txn.import_batch_id == batch.batch_id
        ]
        self.assertTrue(all(
            txn.account_id == balance_account.account_id
            for txn in transactions
        ))

        agent.store.delete_upload_batch("u_001", batch.batch_id)
        self.assertAlmostEqual(balance_account.balance, 0.0, places=2)

    def test_import_creates_upload_batch_and_marks_transactions(self):
        agent = make_agent()
        first = agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        self.assertEqual(first.kind, "confirm")
        imported = agent.handle("确认")
        self.assertEqual(imported.data["imported"], 6)

        batches = agent.store.upload_batches_of("u_001")
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertEqual(batch.record_count, 6)
        self.assertAlmostEqual(batch.total_expense, 5959.40, places=2)
        self.assertEqual(batch.filename, "bill_sample.txt")
        transactions = [
            txn for txn in agent.store.transactions_of("u_001")
            if txn.import_batch_id == batch.batch_id
        ]
        self.assertEqual(len(transactions), 6)
        self.assertTrue(all(txn.source.startswith("upload:") for txn in transactions))
        self.assertTrue(all(not txn.is_demo for txn in transactions))

    def test_deleting_batch_removes_only_that_batch(self):
        agent = make_agent()
        agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        agent.handle("确认")
        batch = agent.store.upload_batches_of("u_001")[0]

        result = agent.store.delete_upload_batch("u_001", batch.batch_id)

        self.assertEqual(result["deleted_transactions"], 6)
        self.assertEqual(agent.store.upload_batches_of("u_001"), [])
        self.assertFalse([
            txn for txn in agent.store.transactions_of("u_001")
            if txn.import_batch_id == batch.batch_id
        ])


class TestUploadBatchService(unittest.TestCase):
    def test_list_and_delete_survive_restart_and_restore_analysis_scope(self):
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
                f"识别这张账单截图 {SAMPLE_TEXT}",
                request_id="upload-batch-scan",
            )
            first.send_message(
                session_id,
                "确认",
                request_id="upload-batch-confirm",
            )
            batches = first.list_uploads(session_id)
            self.assertEqual(len(batches["uploads"]), 1)

            second = AgentSessionService(
                make_store(),
                llm_mode="off",
                store_factory=make_store,
                repository=SQLiteRepository(db_path),
            )
            restored = second.list_uploads(session_id)
            self.assertEqual(len(restored["uploads"]), 1)
            batch_id = restored["uploads"][0]["batch_id"]
            deleted = second.delete_upload(session_id, batch_id)
            self.assertEqual(deleted["deleted_transactions"], 6)

            analysis = second.send_message(
                session_id,
                "分析我这个月的账单",
                request_id="analysis-after-delete",
            )

        self.assertEqual(analysis["reply"]["data"]["data_scope"], "demo")
        self.assertGreater(analysis["reply"]["data"]["stats"]["count"], 6)


if __name__ == "__main__":
    unittest.main()
