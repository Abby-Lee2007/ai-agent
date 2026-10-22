"""Natural-language questions about uploaded bill balance reconciliation."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.support import make_agent


class TestBalanceQuestions(unittest.TestCase):
    def test_upload_balance_mismatch_question_answers_instead_of_bill_analysis(self):
        agent = make_agent()

        reply = agent.handle("上传账单的余额怎么感觉和实际对不上？")

        self.assertEqual(reply.intent, "account.upload_balance_explain")
        self.assertIn("上传账单余额", reply.text)
        self.assertIn("虚拟", reply.text)
        self.assertIn("0.00", reply.text)
        self.assertNotIn("消费分类排行", reply.text)

    def test_upload_balance_question_reports_imported_net_and_batch(self):
        agent = make_agent()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mixed.csv"
            path.write_text(
                "日期,商户,金额,收/支\n"
                "2026-10-12,星巴克咖啡,38.00,支出\n"
                "2026-10-13,退款,100.00,收入\n",
                encoding="utf-8",
            )
            self.assertEqual(agent.handle(f"识别这张账单截图 {path}").kind, "confirm")
            self.assertEqual(agent.handle("确认").data["imported"], 2)

            reply = agent.handle("为什么上传账单余额和实际银行卡余额不一样？")

        self.assertEqual(reply.intent, "account.upload_balance_explain")
        self.assertIn("62.00", reply.text)
        self.assertIn("收入 100.00", reply.text)
        self.assertIn("支出 38.00", reply.text)
        self.assertIn("1 个批次", reply.text)

    def test_plain_upload_balance_query_still_uses_account_balance(self):
        agent = make_agent()

        reply = agent.handle("查一下上传账单余额")

        self.assertEqual(reply.intent, "account.balance")
        self.assertIn("总资产", reply.text)

    def test_upload_balance_scope_question_answers_before_keyword_matching(self):
        agent = make_agent()
        balance = agent.handle("我还有多少钱")
        self.assertEqual(balance.intent, "account.balance")

        reply = agent.handle(
            "前面三个都是虚拟数据的消耗，我的消耗只计入上传账单余额里面吗"
        )

        self.assertEqual(reply.intent, "account.upload_balance_scope")
        self.assertIn("只记入「上传账单余额」", reply.text)
        self.assertIn("不会自动修改", reply.text)
        self.assertIn("账单分析", reply.text)
        self.assertNotIn("总资产 184,060.52", reply.text)

    def test_upload_balance_scope_answer_reports_current_ledger(self):
        agent = make_agent()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scope.csv"
            path.write_text(
                "日期,商户,金额,收/支\n"
                "2026-10-12,星巴克咖啡,38.00,支出\n"
                "2026-10-13,退款,100.00,收入\n",
                encoding="utf-8",
            )
            agent.handle(f"识别这张账单截图 {path}")
            agent.handle("确认")

            reply = agent.handle("上传账单里的消费是不是只影响上传账单余额？")

        self.assertEqual(reply.intent, "account.upload_balance_scope")
        self.assertIn("62.00", reply.text)
        self.assertIn("不会自动修改", reply.text)

    def test_bill_analysis_command_is_not_turned_into_scope_question(self):
        agent = make_agent()

        reply = agent.handle("分析上传账单里的消费")

        self.assertEqual(reply.intent, "bill.analyze")


if __name__ == "__main__":
    unittest.main()
