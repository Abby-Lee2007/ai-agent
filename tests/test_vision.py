"""账单图片识别测试：OCR 文本清洗、记录解析、识别-确认-导入全流程。"""
from __future__ import annotations

import unittest
from pathlib import Path

from tests.support import make_agent
from finagent.vision.receipt_parser import (
    guess_category,
    normalize_line,
    parse_receipt_text,
)
from finagent.vision.receipt_reader import ReceiptReader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_IMAGE = PROJECT_ROOT / "samples" / "bill_sample.png"
SAMPLE_TEXT = PROJECT_ROOT / "samples" / "bill_sample.txt"

# 真实 Windows OCR 输出：中文之间带空格、连字符变成"一"、小数点丢失
NOISY_OCR_TEXT = """
招 商 银 行 信 用 卡 账 单 （ 截 图 样 例 ）
账 单 周 期 2026 一 10 一 01 一 2026 一 10 一 14 卡 号 * * * * 8877
2026 一 10 一 12 10 ： 05 携 程 旅 行 网 一 2480 00
2026 一 10 一 12 18 ： 30 星 巴 克 咖 啡 一 38 00
2026 一 10 一 13 09 ： 10 滴 滴 出 行 一 42 50
本 期 应 还 金 额 5959 40 元
"""

# 某些 OCR 引擎会按列重排：先出所有日期，再出商户和金额
COLUMN_OCR_TEXT = """
2026-10-12
2026-10-13
携程旅行网 2480.00
星巴克咖啡 38.00
"""


class TestReceiptTextCleanup(unittest.TestCase):
    def test_normalize_removes_cjk_spaces(self):
        self.assertEqual(normalize_line("携 程 旅 行 网"), "携程旅行网")

    def test_normalize_repairs_hyphen_lookalike(self):
        self.assertIn("2026-10-12", normalize_line("2026 一 10 一 12"))

    def test_normalize_repairs_lost_decimal_point(self):
        line = normalize_line("星巴克咖啡一38 00")
        self.assertIn("38.00", line)

    def test_normalize_repairs_fullwidth_colon(self):
        self.assertIn("10:05", normalize_line("10 ： 05 携程旅行网"))


class TestReceiptParsing(unittest.TestCase):
    def test_parse_noisy_ocr(self):
        records = parse_receipt_text(NOISY_OCR_TEXT)
        self.assertEqual(len(records), 3)
        first = records[0]
        self.assertEqual(first.date, "2026-10-12")
        self.assertEqual(first.time, "10:05")
        self.assertEqual(first.merchant, "携程旅行网")
        self.assertEqual(first.amount, -2480.00)
        self.assertEqual(first.category, "旅行")

    def test_all_amounts_are_expenses(self):
        records = parse_receipt_text(NOISY_OCR_TEXT)
        self.assertTrue(all(r.amount < 0 for r in records))
        self.assertAlmostEqual(sum(r.amount for r in records), -2560.50, places=2)

    def test_header_and_total_lines_are_ignored(self):
        records = parse_receipt_text(NOISY_OCR_TEXT)
        merchants = [r.merchant for r in records]
        self.assertNotIn("本期应还金额", merchants)
        self.assertTrue(all("账单" not in m for m in merchants))

    def test_column_reflow_fallback(self):
        records = parse_receipt_text(COLUMN_OCR_TEXT)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].date, "2026-10-12")
        self.assertEqual(records[0].amount, -2480.00)

    def test_income_line_keeps_positive_amount(self):
        text = "2026-10-20 工资代发 18500.00 收入"
        records = parse_receipt_text(text)
        self.assertEqual(records[0].amount, 18500.0)

    def test_guess_category(self):
        self.assertEqual(guess_category("星巴克咖啡"), "餐饮")
        self.assertEqual(guess_category("滴滴出行"), "交通")
        self.assertEqual(guess_category("iCloud+ 云存储"), "订阅服务")
        self.assertEqual(guess_category("不认识的店"), "其他")


class TestReceiptReader(unittest.TestCase):
    def setUp(self):
        self.reader = ReceiptReader()

    def test_backends_report(self):
        backends = self.reader.backends()
        self.assertTrue(backends["text_file"])
        self.assertIn("windows_ocr", backends)

    def test_read_text_file(self):
        result = self.reader.read(str(SAMPLE_TEXT))
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, "text_file")
        self.assertEqual(len(result.records), 6)

    @unittest.skipUnless(SAMPLE_IMAGE.exists(), "缺少示例账单图片")
    def test_read_image_with_windows_ocr(self):
        if not self.reader.backends()["windows_ocr"]:
            self.skipTest("本机没有可用的 Windows OCR")
        result = self.reader.read(str(SAMPLE_IMAGE))
        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(len(result.records), 6)
        merchants = {r.merchant for r in result.records}
        self.assertIn("携程旅行网", merchants)
        self.assertAlmostEqual(result.total_expense, 5959.40, places=2)

    def test_missing_file_reports_clearly(self):
        result = self.reader.read("samples/definitely_not_here.png")
        self.assertFalse(result.ok)
        self.assertIn("找不到文件", result.message)

    def test_unsupported_suffix(self):
        result = self.reader.read(str(Path(__file__)))
        self.assertFalse(result.ok)


class TestReceiptToolFlow(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()

    def test_scan_asks_for_path_when_missing(self):
        reply = self.agent.handle("帮我识别这张账单截图")
        self.assertEqual(reply.kind, "question")
        self.assertEqual(self.agent.session.pending.missing, ["image"])

    def test_scan_preview_then_import(self):
        reply = self.agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        self.assertEqual(reply.intent, "bill.scan_receipt")
        self.assertEqual(reply.kind, "confirm")
        self.assertEqual(reply.data["count"], 6)
        self.assertIn("确认", reply.text)
        before = len(self.agent.store.transactions_of("u_001"))
        reply = self.agent.handle("确认")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.data["imported"], 6)
        after = len(self.agent.store.transactions_of("u_001"))
        self.assertEqual(after, before + 6)

    def test_imported_records_detect_duplicate_charge(self):
        self.agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        reply = self.agent.handle("确认")
        self.assertGreaterEqual(reply.data["anomalies"], 1)
        tags = {tag for t in self.agent.store.transactions_of("u_001") for tag in t.tags}
        self.assertIn("receipt_import", tags)

    def test_import_does_not_change_balance(self):
        account = self.agent.store.primary_account("u_001")
        before = account.balance
        self.agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        self.agent.handle("确认")
        self.assertEqual(account.balance, before)

    def test_reimport_is_deduplicated(self):
        self.agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        self.agent.handle("确认")
        self.agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        reply = self.agent.handle("确认")
        self.assertEqual(reply.data["imported"], 0)
        self.assertEqual(reply.data["skipped"], 6)

    def test_cancel_aborts_import(self):
        self.agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        before = len(self.agent.store.transactions_of("u_001"))
        reply = self.agent.handle("取消")
        self.assertIn("取消", reply.text)
        self.assertEqual(len(self.agent.store.transactions_of("u_001")), before)

    def test_import_without_scan(self):
        from finagent.agent.planner import Plan, PlanStep
        agent = make_agent()
        reply = agent._execute(Plan(intent="bill.import_receipt",
                                    steps=[PlanStep("bill.import_receipt")]), agent.context())
        self.assertFalse(reply.ok)
        self.assertIn("没有待导入", reply.text)


if __name__ == "__main__":
    unittest.main()
