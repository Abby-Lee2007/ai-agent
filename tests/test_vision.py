"""账单图片识别测试：OCR 文本清洗、记录解析、识别-确认-导入全流程。"""
from __future__ import annotations

import unittest
import tempfile
import zipfile
from datetime import date
from html import escape
from pathlib import Path

from tests.support import make_agent
from finagent.vision.receipt_parser import (
    ParsedTransaction,
    guess_category,
    looks_like_summary_page,
    normalize_line,
    parse_bill_summary,
    parse_receipt_text,
)
from finagent.vision.receipt_reader import ReceiptReader, ReceiptResult

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

SUMMARY_OCR_TEXT = """
微信支付月账单
2026年10月
本月支出 ¥7,283.34
支出构成
餐饮美食 1,200.00 16.5%
交通出行 800.00 11.0%
购物消费 2,400.00 33.0%
生活缴费 1,200.00 16.5%
娱乐 1,683.34 23.0%
每月支出对比
2026年7月 5,800.00
2026年8月 6,200.00
2026年9月 6,800.00
2026年10月 7,283.34
"""

NOISY_SUMMARY_OCR_TEXT = """
微 信 支 付 月 账 单
2026 年 10 月
本 月 支 出 ¥ 7 ， 283 34
支 出 构 成
餐 饮 美 食 1 ， 200 00 16 5 ％
交 通 出 行 800 00 11 0 ％
购 物 消 费 2 ， 400 00 33 0 ％
生 活 缴 费 1 ， 200 00 16 5 ％
娱 乐 1 ， 683 34 23 0 ％
每 月 支 出 对 比
2026 年 7 月 5 ， 800 00
2026 年 8 月 6 ， 200 00
2026 年 9 月 6 ， 800 00
2026 年 10 月 7 ， 283 34
"""


def _xlsx_cell(ref: str, value: object) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return (
        f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
    )


def make_xlsx(path: Path, rows: list[list[object]]) -> None:
    """Create a tiny XLSX using only Python's standard library."""
    sheet_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = "".join(
            _xlsx_cell(f"{chr(65 + column_index)}{row_number}", value)
            for column_index, value in enumerate(row)
        )
        sheet_rows.append(f'<row r="{row_number}">{cells}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>',
        )
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)


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

    def test_summary_page_is_rejected_before_import(self):
        summary = """
        本月账单
        共支出 7283.34 元
        支出构成
        餐饮 35%
        购物 40%
        每月支出对比
        """

        self.assertTrue(looks_like_summary_page(summary))
        self.assertFalse(looks_like_summary_page(NOISY_OCR_TEXT))


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

    def test_income_summary_label_is_not_imported_as_transaction(self):
        self.assertEqual(
            parse_receipt_text("本月应发总收入 42,727.28"),
            [],
        )

    def test_guess_category(self):
        self.assertEqual(guess_category("星巴克咖啡"), "餐饮")
        self.assertEqual(guess_category("滴滴出行"), "交通")
        self.assertEqual(guess_category("iCloud+ 云存储"), "订阅服务")
        self.assertEqual(guess_category("不认识的店"), "其他")

    def test_parse_summary_breakdown_and_monthly_trend(self):
        summary = parse_bill_summary(SUMMARY_OCR_TEXT)

        self.assertIsNotNone(summary)
        self.assertEqual(summary.period, "2026-10")
        self.assertAlmostEqual(summary.total_expense, 7283.34, places=2)
        self.assertEqual(len(summary.categories), 5)
        categories = {item.category: item.amount for item in summary.categories}
        self.assertAlmostEqual(categories["餐饮"], 1200.0, places=2)
        self.assertAlmostEqual(categories["购物"], 2400.0, places=2)
        self.assertAlmostEqual(categories["娱乐"], 1683.34, places=2)
        self.assertEqual(len(summary.months), 4)
        self.assertEqual(summary.months[-1].month, "2026-10")

    def test_parse_noisy_summary_amounts_and_percentages(self):
        summary = parse_bill_summary(NOISY_SUMMARY_OCR_TEXT)

        self.assertIsNotNone(summary)
        self.assertAlmostEqual(summary.total_expense, 7283.34, places=2)
        categories = {item.category: item.amount for item in summary.categories}
        self.assertAlmostEqual(categories["餐饮"], 1200.0, places=2)
        self.assertAlmostEqual(categories["交通"], 800.0, places=2)
        self.assertAlmostEqual(summary.months[-1].amount, 7283.34, places=2)

    def test_read_excel_transaction_sheet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bill.xlsx"
            make_xlsx(path, [
                ["日期", "商户", "金额", "类别"],
                ["2026-10-12", "星巴克咖啡", -38.0, "餐饮"],
                ["2026-10-13", "滴滴出行", -42.5, "交通"],
            ])
            result = ReceiptReader().read(str(path))

        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(result.backend, "excel_file")
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].merchant, "星巴克咖啡")
        self.assertAlmostEqual(result.total_expense, 80.5, places=2)

    def test_read_excel_summary_sheet(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "summary.xlsx"
            make_xlsx(path, [
                ["月份", "支出"],
                ["2026-07", 5800],
                ["2026-08", 6200],
                ["2026-09", 6800],
                ["2026-10", 7283.34],
                ["类别", "金额", "占比"],
                ["餐饮", 1200, "16.5%"],
                ["购物", 2400, "33.0%"],
            ])
            result = ReceiptReader().read(str(path))

        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(result.backend, "excel_file")
        self.assertIsNotNone(result.summary)
        self.assertAlmostEqual(result.summary.total_expense, 7283.34, places=2)
        self.assertEqual(len(result.summary.months), 4)

    def test_read_wechat_export_with_header_after_notes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "wechat.xlsx"
            rows = [[f"微信支付账单说明 {index}"] for index in range(15)]
            rows.extend([
                ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式", "当前状态"],
                [46317.58802083333, "商户消费", "深圳鸿济生态农业科技有限公司", "先购后付",
                 "支出", "5.5", "零钱", "支付成功"],
                [46317.48384259259, "商户消费", "深圳大学", "听山二楼烤盘饭",
                 "收入", "18.49", "零钱", "支付成功"],
            ])
            make_xlsx(path, rows)
            result = ReceiptReader().read(str(path))

        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(result.backend, "excel_file")
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].date, "2026-10-22")
        self.assertAlmostEqual(result.records[0].amount, -5.5, places=2)
        self.assertAlmostEqual(result.records[1].amount, 18.49, places=2)


class TestReceiptReader(unittest.TestCase):
    def setUp(self):
        self.reader = ReceiptReader()

    def test_backends_report(self):
        backends = self.reader.backends()
        self.assertTrue(backends["text_file"])
        self.assertIn("windows_ocr", backends)

    def test_low_confidence_fields_are_marked_individually(self):
        result = ReceiptResult(
            ok=True,
            records=[ParsedTransaction(
                date="日期不清",
                merchant="未知商户",
                amount=-8.0,
                category="其他",
            )],
        )

        record = result.records[0]
        self.assertIn("date", record.needs_review)
        self.assertIn("merchant", record.needs_review)
        self.assertGreater(record.field_confidence["amount"], 0.8)
        self.assertLess(record.confidence, 0.8)

    def test_multimodal_payload_preserves_field_confidence(self):
        records = self.reader._records_from_payload(
            {
                "records": [{
                    "date": "2026-10-20",
                    "merchant": "清晰商户",
                    "amount": -12.0,
                    "category": "餐饮",
                    "raw_line": "2026-10-20 清晰商户 -12.00",
                    "field_confidence": {
                        "date": 0.99,
                        "merchant": 0.62,
                        "amount": 0.98,
                        "category": 0.9,
                    },
                }]
            },
            date(2026, 10, 28),
        )

        record = records[0]
        self.assertEqual(record.field_confidence["merchant"], 0.62)
        self.assertIn("merchant", record.needs_review)
        self.assertEqual(record.raw_line, "2026-10-20 清晰商户 -12.00")

    def test_receipt_quality_varies_with_field_evidence(self):
        strong = ReceiptResult(
            ok=True,
            records=[ParsedTransaction(
                date="2026-10-12",
                merchant="星巴克咖啡",
                amount=-38.0,
                category="餐饮",
            )],
        )
        weak = ReceiptResult(
            ok=True,
            records=[ParsedTransaction(
                date="",
                merchant="本月应发总收入",
                amount=42727.28,
                category="其他",
                confidence=0.35,
                field_confidence={
                    "date": 0.2,
                    "merchant": 0.5,
                    "amount": 0.8,
                    "category": 0.55,
                },
            )],
        )

        self.assertGreater(strong.quality["score"], weak.quality["score"])
        self.assertGreater(
            strong.quality["average_confidence"],
            weak.quality["average_confidence"],
        )
        self.assertEqual(weak.quality["confidence_basis"], "field_evidence")

    def test_read_text_file(self):
        result = self.reader.read(str(SAMPLE_TEXT))
        self.assertTrue(result.ok)
        self.assertEqual(result.backend, "text_file")
        self.assertEqual(len(result.records), 6)

    def test_read_gbk_csv_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "bill_gbk.csv"
            path.write_bytes(
                (
                    "日期,商户,金额,类别\n"
                    "2026-10-12,星巴克咖啡,-38.00,餐饮\n"
                    "2026-10-13,滴滴出行,-42.50,交通\n"
                ).encode("gb18030")
            )
            result = self.reader.read(str(path))

        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(result.backend, "csv_file")
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].merchant, "星巴克咖啡")
        self.assertAlmostEqual(result.records[0].amount, -38.0, places=2)

    def test_read_wechat_csv_with_trailing_order_numbers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "wechat.csv"
            rows = [
                "微信支付账单明细",
                *[f"说明 {index}" for index in range(15)],
                "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号",
                "2026-10-20 19:52,商户消费,深圳市及时雨农副产品贸易有限公司,先购后付,支出,5.50,零钱,支付成功,4200003221202610227203860857,d8RgepBe3",
                "2026-10-19 18:18,商户消费,星巴克咖啡,咖啡,支出,38.00,零钱,支付成功,4200003142202610202886175202,069b733156d93c8f",
            ]
            path.write_bytes(("\n".join(rows) + "\n").encode("gb18030"))
            result = ReceiptReader().read(str(path))

        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(result.backend, "csv_file")
        self.assertEqual(len(result.records), 2)
        self.assertEqual(result.records[0].merchant, "深圳市及时雨农副产品贸易有限公司")
        self.assertEqual(result.records[0].time, "19:52")
        self.assertAlmostEqual(result.records[0].amount, -5.50, places=2)

    def test_question_mark_merchant_uses_description_or_transfer_label(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "alipay.csv"
            path.write_text(
                "交易时间,交易类型,交易对方,商品说明,收/支,金额\n"
                "2026-10-20 19:52,转账,？？,给王强转账,支出,100.00\n"
                "2026-10-19 18:18,商户消费,？？,星巴克咖啡,支出,38.00\n",
                encoding="utf-8",
            )
            result = ReceiptReader().read(str(path))

        self.assertTrue(result.ok, msg=result.message)
        self.assertEqual(result.records[0].merchant, "转账")
        self.assertEqual(result.records[1].merchant, "星巴克咖啡")

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

    def test_format_question_returns_capabilities_not_analysis(self):
        reply = self.agent.handle("可以上传哪些格式的账单")

        self.assertEqual(reply.intent, "bill.supported_formats")
        self.assertEqual(reply.kind, "executed")
        self.assertIn("PNG", reply.text)
        self.assertIn("XLSX", reply.text)
        self.assertIn("TXT", reply.text)
        self.assertNotIn("本月账单概览", reply.text)

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

    def test_uploaded_bill_switches_analysis_to_uploaded_data_only(self):
        self.agent.handle(f"识别这张账单截图 {SAMPLE_TEXT}")
        imported = self.agent.handle("确认")
        self.assertEqual(imported.data["imported"], 6)

        reply = self.agent.handle("分析我这个月的账单")

        self.assertEqual(reply.intent, "bill.analyze")
        self.assertEqual(reply.data["data_scope"], "uploaded")
        self.assertEqual(reply.data["stats"]["count"], 6)
        self.assertIn("仅用户上传流水", reply.text)
        uploaded = [
            txn for txn in self.agent.store.transactions_of("u_001")
            if "receipt_import" in txn.tags
        ]
        self.assertTrue(uploaded)
        self.assertTrue(all(not txn.is_demo for txn in uploaded))

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

    def test_summary_upload_is_analyzed_without_import_confirmation(self):
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            encoding="utf-8",
            delete=False,
        ) as handle:
            handle.write(SUMMARY_OCR_TEXT)
            path = handle.name
        try:
            reply = self.agent.handle(f"识别这张账单截图 {path}")
        finally:
            Path(path).unlink(missing_ok=True)

        self.assertEqual(reply.intent, "bill.scan_receipt")
        self.assertEqual(reply.kind, "executed")
        self.assertEqual(reply.data["mode"], "summary")
        self.assertIn("消费构成", reply.text)
        self.assertIn("过去月份对比", reply.text)
        self.assertIn("本月支出", reply.text)
        self.assertNotIn("导入流水", reply.text)
        self.assertIsNone(self.agent.session.pending)

    def test_excel_summary_upload_is_analyzed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "summary.xlsx"
            make_xlsx(path, [
                ["月份", "支出"],
                ["2026-07", 5800],
                ["2026-08", 6200],
                ["2026-09", 6800],
                ["2026-10", 7283.34],
                ["类别", "金额", "占比"],
                ["餐饮", 1200, "16.5%"],
                ["购物", 2400, "33.0%"],
            ])
            reply = self.agent.handle(f"识别这张账单截图 {path}")

        self.assertEqual(reply.intent, "bill.scan_receipt")
        self.assertEqual(reply.kind, "executed")
        self.assertEqual(reply.data["mode"], "summary")
        self.assertIn("消费构成", reply.text)
        self.assertIn("过去月份对比", reply.text)
        self.assertIsNone(self.agent.session.pending)

    def test_import_without_scan(self):
        from finagent.agent.planner import Plan, PlanStep
        agent = make_agent()
        reply = agent._execute(Plan(intent="bill.import_receipt",
                                    steps=[PlanStep("bill.import_receipt")]), agent.context())
        self.assertFalse(reply.ok)
        self.assertIn("没有待导入", reply.text)


if __name__ == "__main__":
    unittest.main()
